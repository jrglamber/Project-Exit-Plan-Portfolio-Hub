import json


def _sum_present(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def financial_review_payload(snapshot):
    sources = snapshot.get('sources') or {}
    portfolio = snapshot.get('portfolio') or {}
    out = {'generated_at_utc': snapshot.get('generated_at_utc'), 'strategies': {}}
    for key in ('indices', 'metals', 'bco'):
        data = ((sources.get(key) or {}).get('data') or {})
        accounting = data.get('accounting') or {}
        basket = data.get('basket') or {}
        out['strategies'][key] = {
            'mode': data.get('mode'),
            'nav_gbp': data.get('nav_gbp'),
            'realised_week_gbp': accounting.get('realised_week_gbp'),
            'realised_month_gbp': accounting.get('realised_month_gbp'),
            'realised_all_time_gbp': accounting.get('realised_all_time_gbp'),
            'unrealised_gbp': basket.get('pnl_gbp'),
            'basket_r': basket.get('pnl_r'),
            'open_trades': basket.get('open_trades'),
        }
    # Use the same live scope as the aggregate snapshot and still require the
    # producer itself to report mode=live before it contributes to totals.
    configured_scope = tuple(portfolio.get('scope') or ('indices', 'metals', 'bco'))
    live = tuple(
        k for k in configured_scope
        if k in out['strategies'] and str(out['strategies'][k].get('mode') or '').lower() == 'live'
    )
    out['live_scope'] = list(live)
    out['overall_balance_gbp'] = portfolio.get('nav_gbp')
    out['portfolio_nav_high_water_gbp'] = portfolio.get('nav_high_water_gbp')
    out['portfolio_drawdown_gbp'] = portfolio.get('drawdown_gbp')
    out['open_risk_estimate_gbp'] = portfolio.get('open_risk_estimate_gbp')
    out['balance_source'] = portfolio.get('nav_source')
    out['balance_source_spread_gbp'] = portfolio.get('nav_spread_gbp')
    out['balance_source_disagreement'] = portfolio.get('nav_disagreement')
    out['combined_live_realised_today_gbp'] = portfolio.get('realised_today_gbp')
    out['combined_live_realised_week_gbp'] = _sum_present([out['strategies'][k]['realised_week_gbp'] for k in live])
    out['combined_live_realised_month_gbp'] = _sum_present([out['strategies'][k]['realised_month_gbp'] for k in live])
    out['combined_live_month_total_gbp'] = portfolio.get('month_total_gbp')
    out['combined_live_realised_all_time_gbp'] = _sum_present([out['strategies'][k]['realised_all_time_gbp'] for k in live])
    out['combined_live_unrealised_gbp'] = _sum_present([out['strategies'][k]['unrealised_gbp'] for k in live])
    return out


def financial_review_line(snapshot):
    return 'PEP_FINANCIAL_REVIEW ' + json.dumps(financial_review_payload(snapshot), separators=(',', ':'), default=str)
