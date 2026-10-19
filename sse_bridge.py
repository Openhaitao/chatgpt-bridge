"""Forward visible Responses deltas as Anthropic SSE without buffering a turn."""
import json
import time


class ResponseFailed(Exception):
    pass


def is_token_limit(result):
    return result.get('status') == 'incomplete' and result.get('incomplete_details', {}).get('reason') == 'max_output_tokens'


class Relay:
    def __init__(self, emit):
        self.emit = emit
        self.blocks = {}
        self.next_index = 0
        self.first_content_at = None
        self.result = None

    def block(self, key, value):
        if key not in self.blocks:
            self.blocks[key] = {'index': self.next_index, 'closed': False, 'chars': 0, 'value': value}
            self.next_index += 1
            self.emit('content_block_start', {'type': 'content_block_start', 'index': self.blocks[key]['index'], 'content_block': value})
        return self.blocks[key]

    def delta(self, key, value, text, tool=False):
        block = self.block(key, value)
        if block['closed']:
            raise ResponseFailed('delta-after-closed-block')
        if self.first_content_at is None:
            self.first_content_at = time.monotonic()
        block['chars'] += len(text)
        delta = {'type': 'input_json_delta', 'partial_json': text} if tool else {'type': 'text_delta', 'text': text}
        self.emit('content_block_delta', {'type': 'content_block_delta', 'index': block['index'], 'delta': delta})

    def close(self, key):
        block = self.blocks.get(key)
        if block and not block['closed']:
            block['closed'] = True
            self.emit('content_block_stop', {'type': 'content_block_stop', 'index': block['index']})

    def feed(self, event):
        kind = event.get('type')
        output_index = event.get('output_index', 0)
        if kind == 'response.output_item.added':
            item = event.get('item', {})
            if item.get('type') == 'function_call':
                self.block(('tool', output_index), {'type': 'tool_use', 'id': item['call_id'], 'name': item['name'], 'input': {}})
        elif kind == 'response.output_text.delta':
            self.delta(('text', output_index, event.get('content_index', 0)), {'type': 'text', 'text': ''}, event['delta'])
        elif kind == 'response.function_call_arguments.delta':
            key = ('tool', output_index)
            if key not in self.blocks:
                raise ResponseFailed('tool-delta-without-call-identity')
            self.delta(key, self.blocks[key]['value'], event['delta'], True)
        elif kind == 'response.function_call_arguments.done':
            key = ('tool', output_index)
            if key in self.blocks and not self.blocks[key]['chars']:
                self.delta(key, self.blocks[key]['value'], event.get('arguments', '{}'), True)
        elif kind == 'response.content_part.done':
            self.close(('text', output_index, event.get('content_index', 0)))
        elif kind == 'response.output_item.done':
            key = ('tool', output_index)
            item = event.get('item', {})
            if key in self.blocks and not self.blocks[key]['chars']:
                if 'arguments' in item:
                    self.delta(key, self.blocks[key]['value'], item['arguments'] or '{}', True)
                    self.close(key)
                else:
                    pass  # Defer stop until final arguments are available.
            else:
                self.close(key)
            for key in list(self.blocks):
                if key[0] == 'text' and key[1] == output_index:
                    self.close(key)
        elif kind in ('response.failed', 'response.incomplete', 'error') and not (kind == 'response.incomplete' and is_token_limit(event.get('response', {}))):
            raise ResponseFailed(json.dumps(event.get('response', {}).get('error') or event.get('error') or {'message':'Subscription response failed'}))
        elif kind in ('response.completed', 'response.incomplete'):
            result = event.get('response', {})
            if result.get('status') != 'completed' and not is_token_limit(result):
                raise ResponseFailed('subscription-response-not-completed')
            if is_token_limit(result) and any(item.get('type') == 'function_call' for item in result.get('output', [])):
                raise ResponseFailed('Token limit interrupted a tool call; retry with a larger max_tokens')
            # Some gateways emit only a final object. Support that without replaying
            # blocks already forwarded as deltas (which would duplicate tool calls).
            for oi, item in enumerate(result.get('output', [])):
                if item.get('type') == 'message':
                    for ci, content in enumerate(item.get('content', [])):
                        if content.get('type') != 'output_text':
                            continue
                        key = ('text', oi, ci)
                        if key not in self.blocks:
                            self.delta(key, {'type': 'text', 'text': ''}, content.get('text', ''))
                elif item.get('type') == 'function_call':
                    key = ('tool', oi)
                    value = {'type': 'tool_use', 'id': item['call_id'], 'name': item['name'], 'input': {}}
                    if key not in self.blocks or not self.blocks[key]['chars']:
                        self.delta(key, value, item.get('arguments', '{}'), True)
            for key in list(self.blocks):
                self.close(key)
            self.result = result


def read_response(response, emit=None):
    relay = Relay(emit or (lambda *_: None)) if emit else None
    first_event_at = None
    result = None
    for line in response.iter_lines(chunk_size=1):
        if not line.startswith(b'data: '):
            continue
        try:
            event = json.loads(line[6:])
        except ValueError:
            continue
        if first_event_at is None:
            first_event_at = time.monotonic()
        if relay:
            relay.feed(event)
            result = relay.result
        elif event.get('type') in ('response.completed', 'response.incomplete') and (event.get('response', {}).get('status') == 'completed' or is_token_limit(event.get('response', {}))):
            result = event.get('response')
        elif event.get('type') in ('response.failed', 'response.incomplete', 'error'):
            raise ResponseFailed(json.dumps(event.get('response', {}).get('error') or event.get('error') or {'message':'Subscription response failed'}))
        if result is not None:
            break
    if not result or (result.get('status') != 'completed' and not is_token_limit(result)):
        raise ResponseFailed('subscription-stream-ended-before-completion')
    if is_token_limit(result) and any(item.get('type') == 'function_call' for item in result.get('output', [])):
        raise ResponseFailed('Token limit interrupted a tool call; retry with a larger max_tokens')
    return result, (relay.first_content_at if relay else first_event_at)
