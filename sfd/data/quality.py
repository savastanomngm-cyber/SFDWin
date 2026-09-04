def quality_report(snap, min_contracts=20):
    n = len(snap.contracts)
    def cov(attr):
        return sum(1 for c in snap.contracts if getattr(c, attr) not in (None, 0)) / max(n, 1)
    iv_cov, oi_cov, greek_cov = cov("iv"), cov("open_interest"), cov("gamma")
    expiries = {c.expiry for c in snap.contracts}

    flags = []
    if n < min_contracts:            flags.append("LOW_CONTRACTS")
    if not snap.spot:                flags.append("NO_SPOT")
    if oi_cov < 0.5:                 flags.append("LOW_OI_COVERAGE")
    if iv_cov < 0.5:                 flags.append("LOW_IV_COVERAGE")

    usable = (n >= min_contracts and snap.spot and "LOW_OI_COVERAGE" not in flags)
    return {
        "n_contracts": n, "expiries": len(expiries),
        "iv_coverage": round(iv_cov, 2), "oi_coverage": round(oi_cov, 2),
        "gamma_coverage": round(greek_cov, 2),
        "flags": flags, "usable": usable,
        "provenance": snap.provenance, "source": snap.source,
    }