"""One explicitly approved hosted Qwen call, behind caller-owned durable accounting.

No retries, fallback models, source discovery, tools, or output execution. The
reserve/reconcile callbacks must be backed by atomic durable controller storage.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


API_BASE = 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1'
MODEL = 'qwen3.7-flash-2026-07-15'
REGION = 'singapore-international'
PRICE_REVISION = 'alibaba-singapore-qwen3.7-flash-2026-09-20'
MAX_INPUT_BYTES = 8192
MAX_MESSAGES = 16
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 16384
INPUT_TIER_TOKENS = 32768
OUTPUT_VARIANCE = 10


class QwenError(ValueError):
    """Only fixed codes are exposed; provider bodies and exception text stay private."""
    def __init__(self, code, *, invocation_id=None, charge_status='not_dispatched', settlement=None):
        super().__init__(code)
        self.code = code
        self.invocation_id = invocation_id
        self.charge_status = charge_status
        self.settlement = settlement


@dataclass(frozen=True)
class QwenProfile:
    model: str = MODEL
    region: str = REGION
    price_revision: str = PRICE_REVISION
    input_usd_per_million: Decimal = Decimal('0.030')
    output_usd_per_million: Decimal = Decimal('0.130')
    max_completion_tokens: int = 512
    timeout_seconds: int = 30
    per_call_limit_microusd: int = 10000
    daily_limit_microusd: int = 100000

    def __post_init__(self):
        if (self.model, self.region, self.price_revision) != (MODEL, REGION, PRICE_REVISION):
            raise QwenError('unapproved_profile')
        prices = (self.input_usd_per_million, self.output_usd_per_million)
        if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0 for value in prices):
            raise QwenError('invalid_prices')
        if prices != (Decimal('0.030'), Decimal('0.130')):
            raise QwenError('unapproved_prices')
        for value, low, high in ((self.max_completion_tokens, 1, 512), (self.timeout_seconds, 1, 60),
                                 (self.per_call_limit_microusd, 1, 10000), (self.daily_limit_microusd, 1, 100000)):
            if type(value) is not int or not low <= value <= high:
                raise QwenError('invalid_limits')
        if self.per_call_limit_microusd > self.daily_limit_microusd:
            raise QwenError('invalid_limits')

    def public(self):
        return {'provider': 'alibaba-model-studio', 'model': self.model, 'region': self.region,
                'api_base': API_BASE, 'price_revision': self.price_revision,
                'input_usd_per_million': str(self.input_usd_per_million),
                'output_usd_per_million': str(self.output_usd_per_million),
                'max_completion_tokens': self.max_completion_tokens, 'enable_thinking': False}

    def cost_microusd(self, prompt_tokens, completion_tokens):
        # USD/1M tokens times 1M microUSD/USD cancels. Round up once per call.
        return int((prompt_tokens * self.input_usd_per_million +
                    completion_tokens * self.output_usd_per_million).to_integral_value(rounding=ROUND_CEILING))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _http_post(payload, api_key, timeout):
    # Do not send credentials to redirects or ambient proxy endpoints.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(API_BASE + '/chat/completions', payload, method='POST', headers={
        'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200 or response.headers.get_content_type() != 'application/json':
                raise QwenError('invalid_http_response')
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise QwenError('response_too_large')
            return body
    except HTTPError as error:
        error.close()
        raise QwenError('provider_http_error') from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _messages(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MESSAGES:
        raise QwenError('invalid_messages')
    output, input_bytes = [], 0
    for message in value:
        if (not isinstance(message, dict) or set(message) != {'role', 'content'}
                or message['role'] not in ('system', 'user', 'assistant')
                or not isinstance(message['content'], str)):
            raise QwenError('invalid_messages')
        try:
            input_bytes += len(message['content'].encode('utf-8'))
        except UnicodeError:
            raise QwenError('invalid_messages') from None
        if input_bytes > MAX_INPUT_BYTES:
            raise QwenError('input_too_large')
        output.append({'role': message['role'], 'content': message['content']})
    if not any(message['role'] == 'user' for message in output):
        raise QwenError('user_message_required')
    return output, input_bytes


def _decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise QwenError('invalid_response')
            result[key] = value
        return result
    def constant(value):
        raise QwenError('invalid_response')
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise QwenError('response_too_large')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise QwenError('invalid_response') from None
    if not isinstance(value, dict):
        raise QwenError('invalid_response')
    return value


def _usage(response):
    value = response.get('usage')
    if not isinstance(value, dict):
        raise QwenError('missing_usage')
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    if any(type(value.get(key)) is not int or not 0 <= value[key] <= 2000000 for key in keys):
        raise QwenError('invalid_usage')
    if (not 1 <= value['prompt_tokens'] <= INPUT_TIER_TOKENS
            or value['total_tokens'] != value['prompt_tokens'] + value['completion_tokens']):
        raise QwenError('usage_outside_price_profile')
    details = value.get('completion_tokens_details')
    reasoning = None
    if details is not None:
        if not isinstance(details, dict):
            raise QwenError('invalid_usage')
        reasoning = details.get('reasoning_tokens')
        if reasoning is not None and (type(reasoning) is not int or not 0 <= reasoning <= value['completion_tokens']):
            raise QwenError('invalid_usage')
    # reasoning_tokens is a subset of completion_tokens, not an extra charge.
    return {**{key: value[key] for key in keys}, 'reasoning_tokens': reasoning}


def _answer(response, profile, usage):
    if usage['completion_tokens'] > profile.max_completion_tokens + OUTPUT_VARIANCE:
        raise QwenError('output_token_limit_exceeded')
    choices = response.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise QwenError('invalid_choices')
    choice = choices[0]
    message = choice.get('message')
    if (not isinstance(message, dict) or message.get('role') != 'assistant'
            or not isinstance(message.get('content'), str) or message.get('tool_calls')
            or message.get('function_call') or message.get('refusal')):
        raise QwenError('invalid_answer')
    try:
        size = len(message['content'].encode('utf-8'))
    except UnicodeError:
        raise QwenError('invalid_answer') from None
    if size > MAX_TEXT_BYTES:
        raise QwenError('answer_too_large')
    if usage['reasoning_tokens'] or message.get('reasoning_content'):
        raise QwenError('unexpected_thinking')
    if choice.get('finish_reason') not in ('stop', 'length'):
        raise QwenError('invalid_finish_reason')
    return message['content'], 'truncated' if choice['finish_reason'] == 'length' else 'completed'


class QwenAdapter:
    def __init__(self, api_key, *, reserve, reconcile, profile=None):
        if (not isinstance(api_key, str) or not api_key.startswith('sk-') or api_key.startswith('sk-sp-')
                or not 8 <= len(api_key) <= 256 or not api_key.isascii()
                or any(ord(char) < 33 or ord(char) > 126 for char in api_key)):
            raise QwenError('pay_as_you_go_key_required')
        if not callable(reserve) or not callable(reconcile):
            raise QwenError('durable_budget_callbacks_required')
        self._api_key, self._reserve, self._reconcile = api_key, reserve, reconcile
        self.profile = profile if profile is not None else QwenProfile()
        if type(self.profile) is not QwenProfile:
            raise QwenError('unapproved_profile')

    def _settle(self, invocation_id, settlement):
        try:
            accepted = self._reconcile(invocation_id, settlement)
        except Exception:
            accepted = False
        if accepted is not True:
            raise QwenError('reconciliation_failed', invocation_id=invocation_id,
                            charge_status='uncertain', settlement=settlement) from None

    def complete(self, messages, *, invocation_id, mode):
        if mode == 'local_only':
            raise QwenError('hosted_call_forbidden_in_local_only')
        if mode not in ('hybrid', 'frontier'):
            raise QwenError('explicit_hosted_mode_required')
        if not isinstance(invocation_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', invocation_id):
            raise QwenError('invalid_invocation_id')
        messages, input_bytes = _messages(messages)
        profile = self.profile
        payload = _json({'model': profile.model, 'messages': messages,
                         'max_completion_tokens': profile.max_completion_tokens,
                         'enable_thinking': False, 'stream': False, 'enable_search': False})
        # Reserve the entire first input pricing tier. UTF-8 bytes are not exact
        # tokens; the small text-only envelope leaves substantial template headroom.
        # Reject and retain the reserve if provider usage ever exceeds this tier.
        amount = profile.cost_microusd(INPUT_TIER_TOKENS, profile.max_completion_tokens + OUTPUT_VARIANCE)
        if amount > profile.per_call_limit_microusd:
            raise QwenError('per_call_budget_too_small')
        reservation = {'profile': profile.public(), 'mode': mode,
                       'request_sha256': hashlib.sha256(payload).hexdigest(),
                       'input_bytes': input_bytes, 'input_token_reserve': INPUT_TIER_TOKENS,
                       'output_token_reserve': profile.max_completion_tokens + OUTPUT_VARIANCE,
                       'reserved_microusd': amount, 'per_call_limit_microusd': profile.per_call_limit_microusd,
                       'daily_limit_microusd': profile.daily_limit_microusd}
        try:
            authorized = self._reserve(invocation_id, reservation)
        except Exception:
            raise QwenError('reservation_failed', invocation_id=invocation_id) from None
        if authorized is not True:
            raise QwenError('reservation_denied', invocation_id=invocation_id)

        usage, outcome, text = None, 'uncertain', None
        error_code = None
        try:
            response = _decode(_http_post(payload, self._api_key, profile.timeout_seconds))
            if response.get('model') != profile.model:
                raise QwenError('returned_model_mismatch')
            usage = _usage(response)
            text, outcome = _answer(response, profile, usage)
        except QwenError as error:
            error_code = error.code
        except Exception:
            error_code = 'provider_transport_error'
        actual_cost = profile.cost_microusd(usage['prompt_tokens'], usage['completion_tokens']) if usage else None
        settlement = {'profile': profile.public(), 'outcome': 'failed' if error_code and usage else outcome,
                      'error_code': error_code, 'usage': usage, 'actual_cost_microusd': actual_cost,
                      'cost_basis': 'pinned_list_price_upper_bound' if usage else 'unknown',
                      'reserved_microusd': amount, 'over_reservation': actual_cost is not None and actual_cost > amount}
        self._settle(invocation_id, settlement)
        if error_code:
            raise QwenError(error_code, invocation_id=invocation_id,
                            charge_status='settled' if usage else 'uncertain', settlement=settlement)
        return {'invocation_id': invocation_id, 'text': text, 'model': profile.model, 'status': outcome,
                'usage': usage, 'cost_microusd': actual_cost, 'cost_basis': settlement['cost_basis'],
                'profile': profile.public(), 'settlement': settlement}
