"""SDK Gemini official. Real transports are opt-in, text-only, single-attempt."""
from dataclasses import replace
import json
import logging
import os
import time
from central.phase3.policy import instructions
from central.phase3.transport import live_ready, openai_request
from .credentials import gemini_key
from .policy import Route


def preflight(settings, route, allow_real):
    if settings.mode=='mock':
        return
    if not allow_real or os.getenv('INTEGRATIONS_ENABLE_REAL')!='true' or os.getenv('INTEGRATIONS_PRICES_ACK')!='true':
        raise PermissionError('Chamadas reais da integração bloqueadas')
    if route.provider=='openai':
        live_ready(replace(settings,model=route.model,max_output_tokens=route.max_output_tokens),True)
    else:
        gemini_key()


def build_client(settings):
    from google import genai
    from google.genai import types
    return genai.Client(api_key=gemini_key(),vertexai=False,http_options=types.HttpOptions(
        base_url='https://generativelanguage.googleapis.com',api_version='v1beta',
        timeout=int(settings.timeout_seconds*1000),retry_options=types.HttpRetryOptions(attempts=1)))


def parse_gemini(response):
    candidates=response.candidates or []
    usage=response.usage_metadata
    if len(candidates)!=1 or usage is None:
        return {'code':'invalid'}
    candidate=candidates[0]
    reason=getattr(candidate.finish_reason,'value',candidate.finish_reason)
    if reason!='STOP' or candidate.content is None:
        return {'code':'invalid'}
    texts=[]
    for part in candidate.content.parts or []:
        if getattr(part,'thought',False):
            continue
        if not isinstance(part.text,str) or getattr(part,'function_call',None):
            return {'code':'invalid'}
        texts.append(part.text)
    text='\n'.join(texts)
    i,o,thoughts=usage.prompt_token_count,usage.candidates_token_count,usage.thoughts_token_count or 0
    if any(type(n) is not int or n<0 for n in (i,o,thoughts)) or not text.strip() or len(text.encode())>32768:
        return {'code':'invalid'}
    # Thinking is included in the output reservation/circuit breaker, never treated as free.
    total=getattr(usage,'total_token_count',None)
    billed=max(o+thoughts,total-i if type(total) is int else 0)
    return {'code':'ok','text':text,'input_tokens':i,'output_tokens':billed}


def gemini_request(settings,route,job,allow_real):
    if settings.mode!='live' or route.provider!='gemini':
        raise PermissionError('Transporte real requer rota Gemini e modo live')
    preflight(settings,route,allow_real)
    from google.genai import types
    try:
        with build_client(settings) as client:
            response=client.models.generate_content(model=route.model,contents=job['text'],
                config=types.GenerateContentConfig(system_instruction=instructions(job['agent']),
                    max_output_tokens=route.max_output_tokens,tools=[],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    thinking_config=types.ThinkingConfig(thinking_level='MINIMAL')))
        return parse_gemini(response)
    except Exception as error:
        code=getattr(error,'code',None)
        if code==429:
            return {'code':'rate_limited'}
        if code in (400,401,403,404,422):
            return {'code':'rejected'}
        return {'code':'unknown'}


def child_entry(connection,settings,job,allow_real,simulation):
    # SDK or HTTP debug configuration must not leak credentials/payloads from this process.
    logging.disable(logging.CRITICAL)
    try:
        route=Route(**job['route'])
        if settings.mode=='mock':
            if simulation=='hang':
                time.sleep(3600)
            if simulation=='crash':
                os._exit(2)
            if simulation in ('rate_limited','rejected','invalid','unknown'):
                result={'code':simulation}
            else:
                result={'code':'ok','text':'[SIMULAÇÃO] '+job['agent']+' via '+route.provider,
                        'input_tokens':job['estimate']['estimated_input_tokens'],'output_tokens':16}
        else:
            preflight(settings,route,allow_real)
            if route.provider=='openai':
                routed=replace(settings,model=route.model,max_output_tokens=route.max_output_tokens)
                result=openai_request(routed,job,True)
            else:
                result=gemini_request(settings,route,job,allow_real)
        connection.send_bytes(json.dumps(result).encode())
    except BaseException:
        try:
            connection.send_bytes(b'{"code":"unknown"}')
        except BaseException:
            pass
    finally:
        connection.close()
