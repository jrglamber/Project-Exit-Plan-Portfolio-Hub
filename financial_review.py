import json


def financial_review_payload(snapshot):
    sources = snapshot.get('sources') or {}
    out = {'generated_at_utc': snapshot.get('generated_at_utc'), 'strategies': {}}
    for key in ('indices', 'metals', 'bco'):
        data = ((sources.get(key) or {}).get('data') or {})
        accounting = data.get('accounting') or {}
        basket = data.get('basket') or {}
        out['strategies'][key] = {
            'mode': data.get('mode'),
            'realised_week_gbp': accounting.get('realised_week_gbp'),
            'realised_month_gbp': accounting.get('realised_month_gbp'),
            'unrealised_gbp': basket.get('pnl_gbp'),
            'basket_r': basket.get('pnl_r'),
            'open_trades': basket.get('open_trades'),
        }
    live = [k for k in ('indices','metals') if out['strategies'][k]['mode'] == 'live']
    vals = [out['strategies'][k]['realised_week_gbp'] for k in live]
    out['combined_live_realised_week_gbp'] = sum(v for v in vals if v is not None) if any(v is not None for v in vals) else None
    return out


def financial_review_line(snapshot):
    return 'PEP_FINANCIAL_REVIEW ' + json.dumps(financial_review_payload(snapshot), separators=(',', ':'), default=str)
