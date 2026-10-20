"""Offline-testable request validation and safe upstream diagnostics."""
import re

class InvalidRequest(ValueError):
    pass

def controls(body):
    result = {}
    # Forward the standard Responses cap; subscription support is backend-dependent.
    if 'max_tokens' in body:
        value = body['max_tokens']
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InvalidRequest('max_tokens must be a positive integer')
        result['max_output_tokens'] = value
    if body.get('stop_sequences'):
        raise InvalidRequest('stop_sequences is not supported by the Codex subscription endpoint; omit it. No stop-sequence enforcement is performed.')
    if 'stop_sequences' in body and body['stop_sequences'] != []:
        raise InvalidRequest('stop_sequences must be an empty list or omitted')
    choice = body.get('tool_choice', {'type': 'auto'})
    if not isinstance(choice, dict):
        raise InvalidRequest('tool_choice must be an object')
    typ = choice.get('type')
    mapped = {'auto': 'auto', 'any': 'required', 'none': 'none'}
    if typ == 'tool':
        name = choice.get('name')
        if name not in [t.get('name') for t in body.get('tools', [])]:
            raise InvalidRequest('tool_choice.name must name a supplied tool')
        result['tool_choice'] = {'type': 'function', 'name': name}
    elif typ in mapped:
        if typ == 'any' and not body.get('tools'):
            raise InvalidRequest('tool_choice any requires tools')
        result['tool_choice'] = mapped[typ]
    else:
        raise InvalidRequest('Unsupported tool_choice type')
    if 'disable_parallel_tool_use' in choice:
        if not isinstance(choice['disable_parallel_tool_use'], bool):
            raise InvalidRequest('disable_parallel_tool_use must be boolean')
        result['parallel_tool_calls'] = not choice['disable_parallel_tool_use']
    return result

def image(block):
    source = block.get('source', {})
    if source.get('type') == 'base64':
        url = 'data:' + source['media_type'] + ';base64,' + source['data']
    elif source.get('type') == 'url':
        url = source['url']
    else:
        raise InvalidRequest('Unsupported image source')
    return {'type': 'input_image', 'image_url': url}

def tool_output(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise InvalidRequest('tool_result content must be text or content blocks')
    output = []
    for block in content:
        if block.get('type') == 'text':
            output.append({'type': 'input_text', 'text': block['text']})
        elif block.get('type') == 'image':
            output.append(image(block))
        else:
            raise InvalidRequest('Unsupported tool_result content block')
    return output

def safe_detail(value, secrets=()):
    text = str(value)
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, '[redacted]')
    text = re.sub(r'(?i)Bearer\s+[^\s,;"\']+', 'Bearer [redacted]', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_.-]+)', '[redacted]', text)
    text = re.sub(r'(?i)(access_token|refresh_token|id_token|api_key|authorization|account_id|local_token)(["\']?\s*[:=]\s*["\']?)[^\s,"\'}]+', r'\1\2[redacted]', text)
    return text[:1500]

def upstream_error(response, secrets=()):
    try:
        payload = response.json()
        error = payload.get('error') if isinstance(payload, dict) else None
        if isinstance(error, dict):
            detail = error.get('message') or error.get('detail') or 'Upstream request failed'
        elif isinstance(error, str):
            detail = error
        elif isinstance(payload, dict) and 'detail' in payload:
            detail = payload['detail']
        else:
            detail = 'Upstream request failed'
    except (ValueError, AttributeError):
        detail = 'Upstream returned a non-JSON error (body withheld)'
    status = response.status_code if 400 <= response.status_code <= 599 else 502
    types = {400: 'invalid_request_error', 401: 'authentication_error', 403: 'permission_error', 404: 'not_found_error', 429: 'rate_limit_error', 529: 'overloaded_error'}
    return status, {'type': 'error', 'error': {'type': types.get(status, 'api_error'), 'message': safe_detail(detail, secrets), 'upstream_status': response.status_code}}
