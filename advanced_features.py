"""Additional SolveEase features: deterministic, auditable helpers; stdlib only."""
import json, math, os, re
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "3.12.0"
MIN_PROBABILITY = 0.70
MAX_MODEL_DISAGREEMENT = 0.08
MIN_SELECTIONS = 20
MAX_SELECTIONS = 40


def _num(value, lo=None, hi=None):
    try:
        if isinstance(value, bool) or value is None: return None
        x = float(value)
        if not math.isfinite(x) or (lo is not None and x < lo) or (hi is not None and x > hi): return None
        return x
    except (TypeError, ValueError, OverflowError): return None


def poisson(rate, cap=12):
    rate = _num(rate, 0, 8)
    if rate is None: raise ValueError("rate must be finite and between 0 and 8")
    p = [math.exp(-rate) * rate**k / math.factorial(k) for k in range(cap + 1)]
    s = sum(p)
    return [x/s for x in p]


def dc_score_matrix(home_rate, away_rate, rho=-0.08, cap=10):
    """Dixon-Coles low-score correction; rho must be fitted/validated for production use."""
    h, a = _num(home_rate, .05, 8), _num(away_rate, .05, 8)
    r = _num(rho, -0.5, 0.5)
    if h is None or a is None or r is None: raise ValueError("invalid goal rates or rho")
    hp, ap = poisson(h, cap), poisson(a, cap)
    matrix = [[hp[i]*ap[j] for j in range(cap+1)] for i in range(cap+1)]
    corrections = {(0,0):1-h*a*r, (0,1):1+h*r, (1,0):1+a*r, (1,1):1-r}
    for (i,j), factor in corrections.items(): matrix[i][j] *= max(0.000001, factor)
    total = sum(map(sum, matrix))
    return [[x/total for x in row] for row in matrix]


def shrink_goal_rates(home_goals_for, home_games, home_goals_against, away_goals_for, away_games, away_goals_against, league_home=1.45, league_away=1.15, prior_games=8):
    """Bayesian-style shrinkage of observed rates toward league baselines."""
    vals = [_num(x, 0, 100000) for x in (home_goals_for, home_games, home_goals_against, away_goals_for, away_games, away_goals_against)]
    if any(v is None for v in vals) or vals[1] < 1 or vals[4] < 1: raise ValueError("need non-negative counts and at least one match per team")
    hgf,hg,hga,agf,ag,aga = vals
    h_attack = (hgf + prior_games*league_home)/(hg+prior_games)
    a_defence = (aga + prior_games*league_home)/(ag+prior_games)
    a_attack = (agf + prior_games*league_away)/(ag+prior_games)
    h_defence = (hga + prior_games*league_away)/(hg+prior_games)
    return {"home_goal_rate":round(max(.15,min(4.5,(h_attack+a_defence)/2)),3), "away_goal_rate":round(max(.15,min(4.5,(a_attack+h_defence)/2)),3), "method":"Bayesian-style rate shrinkage toward league baselines", "prior_games":prior_games}


def market_probability(market, selection, home_rate, away_rate, rho=-0.08):
    """Dixon-Coles companion estimate for supported full-time scoreline markets.

    This remains an uncalibrated model. Unsupported settlement families return None.
    """
    matrix = dc_score_matrix(home_rate, away_rate, rho)
    m, s = str(market).lower(), str(selection).strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", s)
    if any(x in m for x in ("corner", "yellow card", "red card", "cards", "offsides", "offside", "shots", "throw ins", "throw-ins", "penalty", "half time", "half-time", "first half", "1st half", "second half", "2nd half", "period 1", "period 2", "player prop", "early goal", "1up", "2up")):
        return None, "Unsupported settlement family; dedicated event/period/player model required"
    cells = [(h, a, p) for h, row in enumerate(matrix) for a, p in enumerate(row)]
    if any(x in m for x in ("btts", "both teams to score", "gg/ng", "gg ng", "ggng")):
        p = sum(v for h,a,v in cells if h > 0 and a > 0)
        if compact in ("yes", "bttsyes", "gg", "ggyes"): return p, "Dixon-Coles score matrix; BTTS yes"
        if compact in ("no", "bttsno", "ng", "ngno"): return 1-p, "Dixon-Coles score matrix; BTTS no"
    if any(x in m for x in ("correct score", "exact score", "full time score")):
        match = re.fullmatch(r"\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\s*", s)
        if match:
            h, a = int(match.group(1)), int(match.group(2))
            return next((v for sh,sa,v in cells if sh == h and sa == a), 0.0), "Dixon-Coles exact-score estimate"
    if any(x in m for x in ("clean sheet", "keep a clean sheet", "team to concede")):
        home_market = any(x in m for x in ("home", "team 1", "1st team"))
        away_market = any(x in m for x in ("away", "team 2", "2nd team"))
        if home_market or away_market:
            yes = sum(v for h,a,v in cells if (a == 0 if home_market else h == 0))
            if compact in ("yes", "true", "will", "keepcleansheet"): return yes, "Dixon-Coles clean-sheet estimate"
            if compact in ("no", "false", "wont", "not"): return 1-yes, "Dixon-Coles clean-sheet estimate"
    if any(x in m for x in ("team to score", "home team score", "away team score", "home to score", "away to score")):
        home_market = any(x in m for x in ("home", "team 1", "1st team"))
        away_market = any(x in m for x in ("away", "team 2", "2nd team"))
        if home_market or away_market:
            yes = sum(v for h,a,v in cells if (h > 0 if home_market else a > 0))
            if compact in ("yes", "willscore"): return yes, "Dixon-Coles team-to-score estimate"
            if compact in ("no", "wontscore"): return 1-yes, "Dixon-Coles team-to-score estimate"
    if any(x in m for x in ("win to nil", "win-to-nil")):
        home_market = any(x in m for x in ("home", "team 1", "1st team"))
        away_market = any(x in m for x in ("away", "team 2", "2nd team"))
        if home_market or away_market:
            yes = sum(v for h,a,v in cells if (h > a and a == 0 if home_market else a > h and h == 0))
            if compact in ("yes", "will"): return yes, "Dixon-Coles win-to-nil estimate"
            if compact in ("no", "wont"): return 1-yes, "Dixon-Coles win-to-nil estimate"
    if any(x in m for x in ("odd/even", "odd even", "total goals odd", "total goals even", "goals odd or even")):
        odd = sum(v for h,a,v in cells if (h+a)%2 == 1)
        if compact in ("odd", "totalodd", "goalsodd"): return odd, "Dixon-Coles total-goals parity estimate"
        if compact in ("even", "totaleven", "goalseven"): return 1-odd, "Dixon-Coles total-goals parity estimate"
    if any(x in m for x in ("home team total", "away team total", "home goals", "away goals", "team goals", "team total goals")):
        home_market = any(x in m for x in ("home", "team 1", "1st team"))
        away_market = any(x in m for x in ("away", "team 2", "2nd team"))
        match = re.search(r"(\d+(?:\.\d+)?)", m+" "+s)
        if (home_market or away_market) and match and (s.startswith("over") or s.startswith("under")):
            line=float(match.group(1)); over=sum(v for h,a,v in cells if (h if home_market else a)>line); under=sum(v for h,a,v in cells if (h if home_market else a)<line)
            return (over if s.startswith("over") else under), f"Dixon-Coles team-total estimate at {line:g}"
    if any(x in m for x in ("1x2", "match result", "full time result", "double chance", "winner", "result")):
        hw=sum(v for h,a,v in cells if h>a); dr=sum(v for h,a,v in cells if h==a); aw=sum(v for h,a,v in cells if h<a)
        opts={"home":hw,"home win":hw,"homewin":hw,"1":hw,"draw":dr,"x":dr,"tie":dr,"away":aw,"away win":aw,"awaywin":aw,"2":aw,"1x":hw+dr,"home/draw":hw+dr,"home or draw":hw+dr,"homedraw":hw+dr,"x2":dr+aw,"draw/away":dr+aw,"draw or away":dr+aw,"drawaway":dr+aw,"12":hw+aw,"home or away":hw+aw,"homeoraway":hw+aw,"no draw":hw+aw,"nodraw":hw+aw}
        if s in opts: return opts[s], "Dixon-Coles score matrix; uncalibrated model estimate"
    match=re.search(r'(\d+(?:\.\d+)?)', m+" "+s)
    if match and any(x in m for x in ("over", "under", "total", "goal")):
        line=float(match.group(1)); over=sum(v for h,a,v in cells if h+a>line); under=sum(v for h,a,v in cells if h+a<line)
        if s.startswith("over") or "over" in m: return over, f"Dixon-Coles estimate; over {line:g}"
        if s.startswith("under") or "under" in m: return under, f"Dixon-Coles estimate; under {line:g}"
    return None, "Unsupported market/selection; no probability assigned"


def implied_probability(odds):
    x=_num(odds,1.000001,10000)
    return None if x is None else 1/x


def remove_overround(outcome_odds):
    probs=[implied_probability(x) for x in outcome_odds]
    if not probs or any(x is None for x in probs): return {"ok":False,"reason":"Complete valid odds for every outcome are required."}
    total=sum(probs)
    return {"ok":True,"raw_implied_probabilities":probs,"overround":round(total-1,6),"margin_removed_probabilities":[round(x/total,6) for x in probs]}


def calibration_metrics(predictions, outcomes):
    if not isinstance(predictions,list) or not isinstance(outcomes,list) or not predictions or len(predictions)!=len(outcomes):
        raise ValueError("predictions and outcomes must be non-empty lists of equal length")
    pairs=[]
    for p,y in zip(predictions,outcomes):
        p=_num(p,0,1); y=_num(y,0,1)
        if p is None or y not in (0.0,1.0): raise ValueError("probabilities must be 0..1 and outcomes must be 0 or 1")
        pairs.append((p,y))
    eps=1e-15
    return {"n":len(pairs),"brier_score":sum((p-y)**2 for p,y in pairs)/len(pairs),"log_loss":-sum(y*math.log(max(eps,p))+(1-y)*math.log(max(eps,1-p)) for p,y in pairs)/len(pairs),"note":"Metrics describe only the supplied sample; they do not establish future calibration."}


def psychology_flags(selection):
    """Diagnostic-only bias flags; never alters probabilities."""
    text=" ".join(str(selection.get(k,"")) for k in ("reasoning","narrative","notes","event","selection")).lower()
    flags=[]
    if any(x in text for x in ("last match", "last game", "recent win", "recent loss", "on a run")):
        flags.append({"flag":"recency_bias_check","severity":"review","detail":"Compare recent results with longer opponent-adjusted home/away form."})
    if any(x in text for x in ("big club", "big team", "famous", "reputation", "giant")):
        flags.append({"flag":"reputation_bias_check","severity":"review","detail":"Check current performance instead of relying on club reputation."})
    if any(x in text for x in ("revenge", "derby", "must win", "must-win", "motivation")):
        flags.append({"flag":"narrative_bias_check","severity":"review","detail":"Treat narrative as unverified unless supported by measurable evidence."})
    if any(x in text for x in ("due to win", "due a win", "can't lose forever", "cannot lose forever", "recover losses", "chase losses")):
        flags.append({"flag":"gambler_fallacy_or_chasing","severity":"warning","detail":"Past losses do not make a win due; avoid chasing losses."})
    if _num(selection.get("odds_movement"), -1000, 1000) is not None:
        flags.append({"flag":"odds_movement_available","severity":"review","detail":"Movement is only interpretable with timestamped prices and source history; it does not prove insider information."})
    if _num(selection.get("consensus_percent"),0,100) is not None:
        flags.append({"flag":"herd_consensus_check","severity":"review","detail":"Consensus can be wrong; verify the source and independent evidence."})
    return flags


def eligible_selection(selection, threshold=MIN_PROBABILITY):
    p=_num(selection.get("estimated_probability"),0,1)
    quality=str(selection.get("evidence_quality", "")).lower()
    quality_ok=quality in ("adequate","high","verified","calibrated") or selection.get("calibrated") is True
    return p is not None and p >= threshold and quality_ok


def _fixture_key(event):
    text = re.sub(r"[^a-z0-9]+", " ", str(event or "").lower()).strip()
    parts = re.split(r"\s+(?:vs?\.?|versus|v\.?|at)\s+", text, maxsplit=1)
    return tuple(part.strip() for part in parts) if len(parts) == 2 else (text, "")


def replacement_candidate_eligible(candidate, original, threshold=MIN_PROBABILITY, max_disagreement=MAX_MODEL_DISAGREEMENT, now=None):
    """Strict gate for externally supplied replacements; unknown/stale prices never qualify."""
    if not isinstance(candidate, dict) or not isinstance(original, dict): return False
    if _fixture_key(candidate.get("event")) != _fixture_key(original.get("event")): return False
    if not all(_fixture_key(candidate.get("event"))): return False
    if candidate.get("odds_verified") is not True: return False
    if not candidate.get("bookmaker_key") or not candidate.get("bookmaker") or not candidate.get("odds_source"): return False
    odds = _num(candidate.get("odds"), 1.01, 1000)
    p1 = _num(candidate.get("model_probability"), 0, 1)
    p2 = _num(candidate.get("alternate_model_probability"), 0, 1)
    if odds is None or p1 is None or p2 is None: return False
    conservative = min(p1, p2)
    if conservative < threshold or abs(p1-p2) > max_disagreement: return False
    if conservative * odds - 1 <= 0: return False
    stamp = candidate.get("updated_at")
    if not isinstance(stamp, str) or not stamp.strip(): return False
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
        age = (now.astimezone(timezone.utc)-dt.astimezone(timezone.utc)).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return False
    freshness_limit = max(30, int(os.environ.get("FEED_STALE_AFTER_SECONDS", "300")))
    if age < -30 or age > freshness_limit: return False
    # Odds may be lower than the original; value/probability gates decide, not price alone.
    return True


def optimize_ticket(legs, threshold=MIN_PROBABILITY):
    if not isinstance(legs,list) or not legs: raise ValueError("legs must be a non-empty list")
    optimized=[]; excluded=[]; replaced=[]
    for leg in legs[:200]:
        if not isinstance(leg,dict): continue
        original=dict(leg.get("original", leg))
        fixture=str(original.get("event", "Unknown fixture"))
        candidates=leg.get("candidates", [])
        all_candidates=[dict(c) for c in candidates if isinstance(c,dict)] if isinstance(candidates,list) else []
        qualifying=[c for c in all_candidates if replacement_candidate_eligible(c, original, threshold)]
        original_p = _num(original.get("estimated_probability"), 0, 1)
        original_odds = _num(original.get("odds"), 1.01, 1000)
        original_edge = _num(original.get("estimated_edge_percent"), -100000, 100000)
        original_ok = (eligible_selection(original, threshold) and original_odds is not None
                       and original.get("odds_verified") is True and original_edge is not None and original_edge > 0)
        if not qualifying:
            if original_ok:
                best=dict(original); best["status"]="retained"
                optimized.append({"event":fixture,"selection":best,"status":"retained"})
            else:
                excluded.append({"event":fixture,"original":original,"status":"excluded",
                                 "reason":"No original selection or replacement passed the probability, positive-value, source-provenance and fresh-price gates."})
            continue
        best=max(qualifying,key=lambda c: (min(float(c["model_probability"]),float(c["alternate_model_probability"]))*float(c["odds"])-1,
                                           min(float(c["model_probability"]),float(c["alternate_model_probability"]))))
        best=dict(best); best["estimated_probability"]=min(float(best["model_probability"]),float(best["alternate_model_probability"]))
        best["estimated_edge_percent"]=(best["estimated_probability"]*float(best["odds"])-1)*100
        best["status"]="replaced"
        replaced.append({"event":fixture,"original":original,"replacement":best,"status":"replaced"})
        optimized.append({"event":fixture,"selection":best,"status":"replaced"})
    return {"ok":True,"threshold":threshold,"original_legs_received":len(legs),"optimized_legs":optimized,
            "replacements":replaced,"exclusions":excluded,"qualifying_count":len(optimized),
            "note":"Replacement candidates require exact same-fixture identity, configured bookmaker/source, verified odds, fresh timestamp, two supported model estimates agreeing within 8 percentage points, at least 70% conservative probability and positive estimated value. Lower odds are not automatically rejected. Estimates are uncalibrated; no bet is placed."}


def build_daily_accumulator(fixtures, local_date=None, timezone_name=None, target=MIN_SELECTIONS, maximum=MAX_SELECTIONS, threshold=MIN_PROBABILITY, now=None):
    tz_name=timezone_name or os.environ.get("APP_TIMEZONE","Africa/Lagos")
    try: tz=ZoneInfo(tz_name)
    except ZoneInfoNotFoundError: raise ValueError("Unknown APP_TIMEZONE; use a valid IANA timezone such as Africa/Lagos")
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None: now=now.replace(tzinfo=timezone.utc)
    today=local_date or now.astimezone(tz).date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",today): raise ValueError("date must use YYYY-MM-DD")
    target=max(1,min(int(target),MAX_SELECTIONS)); maximum=max(1,min(int(maximum),MAX_SELECTIONS)); target=min(target,maximum)
    current=[]; excluded=[]; seen=set()
    for fixture in fixtures if isinstance(fixtures,list) else []:
        if not isinstance(fixture,dict): continue
        event=str(fixture.get("event", "Unknown fixture")); start=fixture.get("start_time") or fixture.get("commence_time")
        try:
            dt=datetime.fromisoformat(str(start).replace("Z","+00:00"))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
            local_dt=dt.astimezone(tz)
        except Exception:
            excluded.append({"event":event,"reason":"Missing or invalid fixture start time; excluded rather than guessed."}); continue
        if local_dt.date().isoformat()!=today:
            continue
        if local_dt <= now.astimezone(tz):
            excluded.append({"event":event,"reason":"Already started or completed; excluded from pre-match accumulator."}); continue
        candidates=fixture.get("selections",[]) if isinstance(fixture.get("selections",[]),list) else []
        q=[dict(c) for c in candidates if isinstance(c,dict) and eligible_selection(c,threshold)]
        if not q:
            excluded.append({"event":event,"reason":"No qualifying alternative found for this fixture."}); continue
        q.sort(key=lambda c: (_num(c.get("estimated_probability"),0,1) or 0),reverse=True)
        pick=q[0]; key=str(fixture.get("event_id") or (event+"|"+str(pick.get("market"))+"|"+str(pick.get("selection")))).lower()
        if key in seen: continue
        seen.add(key); current.append({"event":event,"start_time":local_dt.isoformat(),"market":pick.get("market"),"selection":pick.get("selection"),"odds":_num(pick.get("odds"),1.000001,10000),"estimated_probability":pick.get("estimated_probability"),"evidence_quality":pick.get("evidence_quality"),"reasoning":pick.get("reasoning","Evidence summary not supplied"),"psychology_flags":psychology_flags(pick)})
        if len(current)>=maximum: break
    combined=1.0
    for item in current: combined*=float(item["estimated_probability"])
    return {"ok":True,"date":today,"timezone":tz_name,"target_count":target,"maximum_count":maximum,"threshold":threshold,"qualifying_count":len(current),"shortfall":max(0,target-len(current)),"meets_target":len(current)>=target,"selections":current,"excluded_or_unavailable":excluded,"combined_probability_independence_assumption":combined if current else None,"combined_probability_note":"Naive independence product shown for reference only; football selections can be dependent and this is not a calibrated accumulator probability.","status":"target met" if len(current)>=target else f"Only {len(current)} qualifying selections; shortfall {target-len(current)}. Criteria were not relaxed."}


def fetch_fixture_feed(url=None, timeout=8):
    url=(url or os.environ.get("FIXTURES_FEED_URL","")).strip()
    if not url: return {"ok":False,"error":"FIXTURES_FEED_URL is not configured. Supply a permitted fixture feed or post fixture data to the daily scan endpoint."}
    if not url.startswith("https://"): return {"ok":False,"error":"Fixture feed must use HTTPS."}
    try:
        req=Request(url,headers={"User-Agent":"SolveEase/3.8.0 (fixture source; contact site operator)","Accept":"application/json"})
        with urlopen(req,timeout=timeout) as r:
            data=r.read(2_000_000)
        payload=json.loads(data.decode("utf-8"))
        fixtures=payload.get("fixtures",payload.get("events",[])) if isinstance(payload,dict) else payload
        if not isinstance(fixtures,list): return {"ok":False,"error":"Feed JSON must be a list or contain fixtures/events array."}
        return {"ok":True,"fixtures":fixtures,"source":"configured HTTPS fixture feed","count":len(fixtures)}
    except Exception as e:
        return {"ok":False,"error":f"Fixture feed unavailable or invalid ({type(e).__name__}); no fixtures were invented."}


def telegram_send(text, timeout=8):
    token=os.environ.get("TELEGRAM_BOT_TOKEN","").strip(); chat=os.environ.get("TELEGRAM_CHAT_ID","").strip()
    if not token or not chat: return {"ok":False,"configured":False,"error":"Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the private deployment environment."}
    if len(text)>3900: text=text[:3850]+"\n…report shortened"
    body=json.dumps({"chat_id":chat,"text":text,"disable_web_page_preview":True}).encode()
    req=Request("https://api.telegram.org/bot"+token+"/sendMessage",data=body,headers={"Content-Type":"application/json"},method="POST")
    try:
        with urlopen(req,timeout=timeout) as r: result=json.loads(r.read(200_000).decode())
        return {"ok":bool(result.get("ok")),"configured":True,"delivered":bool(result.get("ok")),"description":result.get("description","Telegram API response")}
    except Exception as e: return {"ok":False,"configured":True,"delivered":False,"error":f"Telegram delivery failed ({type(e).__name__}); inspect private service logs."}


def format_daily_alert(report):
    lines=["SolveEase · Daily accumulator scan",f"Date: {report.get('date')} ({report.get('timezone')})",f"Qualifying selections: {report.get('qualifying_count',0)}/{report.get('target_count',20)} target (max {report.get('maximum_count',40)})",f"Status: {report.get('status','unknown')}"]
    for i,s in enumerate(report.get("selections",[]),1):
        p=_num(s.get("estimated_probability"),0,1)
        lines.append(f"{i}. {s.get('event')} — {s.get('market')} / {s.get('selection')} | odds {s.get('odds') or 'unavailable'} | estimate {p*100:.1f}%" if p is not None else f"{i}. {s.get('event')} — {s.get('market')} / {s.get('selection')} | probability unavailable")
    if report.get("excluded_or_unavailable"): lines.append(f"Excluded/unavailable fixtures: {len(report['excluded_or_unavailable'])}")
    lines.append("Estimates are not guarantees. No bets are placed.")
    return "\n".join(lines)
