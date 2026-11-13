import unittest

from central.mdm import (
    MdmAuditStore,
    MdmHub,
    RawCustomerRecord,
    build_holding_demo_records,
    is_valid_cnpj,
    is_valid_cpf,
    mask_document_lgpd,
    normalize_address,
    normalize_document,
    normalize_name,
    normalize_phone,
)


class MdmStandardizationTests(unittest.TestCase):
    def test_valid_and_invalid_cpf_cnpj(self):
        self.assertTrue(is_valid_cpf("39053344705"))
        self.assertTrue(is_valid_cpf("52998224725"))
        self.assertFalse(is_valid_cpf("11111111111"))
        self.assertFalse(is_valid_cpf("39053344700"))
        self.assertTrue(is_valid_cnpj("11222333000181"))
        self.assertFalse(is_valid_cnpj("11222333000199"))

        digits, ok, kind = normalize_document("390.533.447-05")
        self.assertEqual((digits, ok, kind), ("39053344705", True, "CPF"))
        self.assertEqual(mask_document_lgpd(digits), "***.533.447-**")

    def test_normalization_helpers(self):
        self.assertEqual(normalize_name("João Ávila-Costa!"), "JOAO AVILA COSTA")
        self.assertEqual(normalize_phone("(11) 98765-4321"), "+5511987654321")
        self.assertEqual(normalize_address("Av. Paulista, 1578 - Bela Vista"), "AVENIDA PAULISTA 1578 BELA VISTA")


class MdmHubPipelineTests(unittest.TestCase):
    def test_holding_deduplication_and_golden_record_coexistence(self):
        store = MdmAuditStore(":memory:")
        try:
            hub = MdmHub(audit_store=store)
            hub.ingest(build_holding_demo_records())
            report = hub.run_pipeline(sync_mode="coexistencia", strategy="confianca_recencia")

            self.assertEqual(report.raw_records_count, 5)
            self.assertEqual(report.golden_records_count, 2)
            self.assertAlmostEqual(report.deduplication_rate, 0.6, places=2)
            self.assertGreater(report.avg_quality_after, report.avg_quality_before)
            self.assertTrue(report.sync_back_events)

            # Audit store never stores raw CPF digits
            lineage_rows = store.db.execute("SELECT COUNT(*) FROM mdm_lineage").fetchone()[0]
            self.assertEqual(lineage_rows, 5)
            runs = store.db.execute("SELECT golden_count FROM mdm_runs").fetchone()[0]
            self.assertEqual(runs, 2)

            api_payload = report.golden_records[0].to_api_360(mask_pii=True)
            self.assertIn("***", api_payload["cliente"]["documento"])
            self.assertIn("***", api_payload["cliente"]["email"])
        finally:
            store.close()

    def test_centralized_vs_consolidation_sync_modes(self):
        records = [
            RawCustomerRecord(
                source_system="ERP_Alfa",
                source_record_id="E1",
                holding_company="Alfa",
                full_name="Carlos Eduardo Silva",
                document="39053344705",
                email="carlos@empresa.com",
                phone="11999998888",
                address="Av. Brasil, 100",
                updated_at="2026-11-10T10:00:00Z",
            ),
            RawCustomerRecord(
                source_system="Legado_Antigo",
                source_record_id="L1",
                holding_company="Beta",
                full_name="Carlos Silva",
                document="39053344705",
                email="carlos.antigo@provedor.com",
                phone="",
                address="Av Brasil 100",
                updated_at="2023-01-01T10:00:00Z",
            ),
        ]
        hub = MdmHub()
        hub.ingest(records)

        # Consolidação nunca escreve de volta para as origens (somente leitura)
        cons_report = hub.run_pipeline(sync_mode="consolidacao")
        self.assertEqual(len(cons_report.sync_back_events), 0)

        # Coexistência preenche lacunas (phone) sem sobrescrever email existente
        coex_report = hub.run_pipeline(sync_mode="coexistencia")
        self.assertEqual(len(coex_report.sync_back_events), 1)
        self.assertEqual(coex_report.sync_back_events[0].changed_fields, {"phone": "+5511999998888"})

        # Centralizado impõe nome e email vencedores ao sistema legado
        cent_report = hub.run_pipeline(sync_mode="centralizado")
        self.assertEqual(len(cent_report.sync_back_events), 1)
        self.assertIn("email", cent_report.sync_back_events[0].changed_fields)
        self.assertIn("phone", cent_report.sync_back_events[0].changed_fields)

    def test_document_conflict_triggers_stewardship(self):
        hub = MdmHub()
        hub.ingest(
            [
                RawCustomerRecord(
                    source_system="ERP_Alfa",
                    source_record_id="E10",
                    holding_company="Alfa",
                    full_name="Ana Maria Souza",
                    document="39053344705",
                    email="ana.souza@empresa.com",
                ),
                RawCustomerRecord(
                    source_system="CRM_Beta",
                    source_record_id="C20",
                    holding_company="Beta",
                    full_name="Ana Maria Souza",
                    document="52998224725",
                    email="ana.souza@empresa.com",
                ),
                RawCustomerRecord(
                    source_system="Legado_Gamma",
                    source_record_id="L30",
                    holding_company="Gamma",
                    full_name="Ana Maria Souza",
                    document="11144477735",
                    email="outra.ana.totalmente.distinta@provedor.net",
                    phone="21977776666",
                ),
            ]
        )
        report = hub.run_pipeline()
        self.assertEqual(report.golden_records_count, 3)
        # Apenas E10 vs C20 (mesmo nome + mesmo e-mail + CPFs distintos) vai para Stewardship; L30 e homonimo distinto.
        self.assertEqual(len(report.stewardship_queue), 1)
        self.assertIn("Conflito", report.stewardship_queue[0].reason)


if __name__ == "__main__":
    unittest.main()
