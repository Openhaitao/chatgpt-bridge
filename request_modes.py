"""Translate Claude runtime controls independently: reasoning is not speed."""
class InvalidMode(ValueError): pass

def request_modes(body):
    config = body.get('output_config', {})
    if not isinstance(config, dict):
        raise InvalidMode('output_config must be an object')
    effort = config.get('effort')
    if effort is None:
        effort = 'low'  # Preserve pre-existing behavior when client supplies no setting.
    if not isinstance(effort, str) or effort not in {'low', 'medium', 'high', 'xhigh', 'max'}:
        raise InvalidMode('Unsupported reasoning effort; use low, medium, high, xhigh, or max')
    speed = body.get('speed', 'standard')
    if not isinstance(speed, str) or speed not in {'standard', 'fast'}:
        raise InvalidMode('Unsupported speed; use standard or fast')
    result = {'reasoning': {'effort': effort}, 'service_tier': 'default'}
    if speed == 'fast':
        result['service_tier'] = 'priority'  # Official Codex Fast wire value. Echoed tier is not authoritative.
    return result
