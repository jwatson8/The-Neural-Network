"""One fixed promotion policy shared by training and automatic deployment."""
import math

POLICY = 'ndcg20-bootstrap-v1'


def rejection_reasons(comparison, drift, min_users=100):
    reasons = []
    if comparison.get('groups', {}).get('all', {}).get('users', 0) < max(100, min_users):
        reasons.append('Insufficient held-out users with eligible positive ratings')
    lower = comparison.get('paired_delta_lower_95')
    if lower is None or not math.isfinite(lower) or lower <= 0:
        reasons.append('No positive one-sided 95% paired bootstrap lower bound for NDCG improvement')
    latency = comparison.get('candidate_p95_ms')
    if latency is None or not math.isfinite(latency) or latency >= 600 or latency < 0:
        reasons.append('Candidate inference check exceeds 600ms or has no valid samples')
    if any(drift.get(k, {}).get('status') != 'no_drift' for k in ('events', 'ratings')):
        reasons.append('Drift detected or insufficient drift evidence; investigate before promotion')
    return reasons
