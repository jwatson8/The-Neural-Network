"""Pure user-level inference. Missing values and undefined tests stay explicit."""

import math
import numpy as np
from scipy import stats


def _values(values, *, binary=False):
    valid, excluded = [], 0
    for value in values:
        try:
            number = float(value) if value is not None else float('nan')
        except (ValueError, TypeError, OverflowError):
            number = float('nan')
        if not math.isfinite(number) or (binary and number not in (0, 1)) or (not binary and isinstance(value, bool)):
            excluded += 1
        else:
            valid.append(number)
    return np.asarray(valid, dtype=float), excluded


def welch(control, treatment):
    """Welch two-sided t test and Satterthwaite 95% CI for Treatment-Control."""
    c, ec = _values(control)
    t, et = _values(treatment)
    mc, mt = (float((c/len(c)).sum()) if len(c) else None), (float((t/len(t)).sum()) if len(t) else None)
    difference = mt-mc if mc is not None and mt is not None else None
    if difference is not None and not math.isfinite(difference):
        difference = None
    result = dict(method='Welch independent user means; two-sided t; Satterthwaite CI', confidence=.95,
                  control_n=len(c), treatment_n=len(t), excluded_control=ec, excluded_treatment=et,
                  control_mean=mc, treatment_mean=mt, difference=difference,
                  relative_difference=(difference/mc if difference is not None and mc != 0 and math.isfinite(difference/mc) else None),
                  ci95=None, p_value=None, status='insufficient_data')
    if min(len(c), len(t)) < 2:
        return result
    with np.errstate(over='ignore', invalid='ignore'):
        vc, vt = float(c.var(ddof=1)/len(c)), float(t.var(ddof=1)/len(t))
    variance = vc+vt
    if not math.isfinite(variance) or difference is None or not math.isfinite(difference):
        result.update(status='invalid_numeric_range', control_mean=None, treatment_mean=None,
                      difference=None, relative_difference=None)
        return result
    if variance <= 0:
        result['status'] = 'undefined_zero_variance'
        return result
    df = 1 / ((vc/variance)**2/(len(c)-1)+(vt/variance)**2/(len(t)-1))
    se = math.sqrt(variance)
    margin = float(stats.t.ppf(.975, df))*se
    if not all(math.isfinite(value) for value in (difference-margin, difference+margin)):
        result['status'] = 'invalid_numeric_range'
        return result
    result.update(status='ok', degrees_of_freedom=df,
                  ci95=[difference-margin, difference+margin],
                  p_value=float(2*stats.t.sf(abs(difference/se), df)))
    return result


def bootstrap_difference(control, treatment, *, seed=42, repetitions=2000):
    """Independent user resampling; percentile CI is exploratory for sparse/skewed costs."""
    if type(seed) is not int or seed < 0 or type(repetitions) is not int or not 100 <= repetitions <= 5000:
        raise ValueError('bootstrap needs a nonnegative seed and 100..5000 repetitions')
    c, ec = _values(control)
    t, et = _values(treatment)
    result = dict(method='Independent user percentile bootstrap; robustness analysis', confidence=.95,
                  seed=seed, repetitions=repetitions, control_n=len(c), treatment_n=len(t),
                  excluded_control=ec, excluded_treatment=et, ci95=None, status='insufficient_data',
                  limitation='Does not repair missingness/selection bias; degenerate empirical CI is not population certainty.')
    if min(len(c), len(t)) < 2:
        return result
    rng = np.random.default_rng(seed)
    differences = np.array([rng.choice(t, len(t), replace=True).mean()-rng.choice(c, len(c), replace=True).mean()
                            for _ in range(repetitions)])
    if not np.isfinite(differences).all():
        result['status'] = 'invalid_numeric_range'
        return result
    result.update(status='empirical_degenerate' if np.ptp(differences) == 0 else 'ok',
                  ci95=[float(value) for value in np.quantile(differences, [.025, .975])])
    return result


def participation(control, treatment):
    """Fisher exact p-value and Newcombe-Wilson 95% risk-difference CI, at user level."""
    c, ec = _values(control, binary=True)
    t, et = _values(treatment, binary=True)
    result = dict(method='Fisher exact two-sided; Newcombe-Wilson independent proportion difference CI',
                  confidence=.95, control_n=len(c), treatment_n=len(t), excluded_control=ec, excluded_treatment=et,
                  control_successes=int(c.sum()), treatment_successes=int(t.sum()), control_mean=None,
                  treatment_mean=None, difference=None, relative_difference=None, ci95=None, p_value=None,
                  status='insufficient_data')
    if not len(c) or not len(t):
        return result
    pc, pt = float(c.mean()), float(t.mean())

    def wilson(p, n):
        z = float(stats.norm.ppf(.975))
        center = (p+z*z/(2*n))/(1+z*z/n)
        half = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
        return center-half, center+half

    lc, uc = wilson(pc, len(c))
    lt, ut = wilson(pt, len(t))
    difference = pt-pc
    result.update(status='ok', control_mean=pc, treatment_mean=pt, difference=difference,
                  relative_difference=difference/pc if pc else None,
                  ci95=[max(-1, difference-math.hypot(pt-lt, uc-pc)), min(1, difference+math.hypot(ut-pt, pc-lc))],
                  p_value=float(stats.fisher_exact([[int(t.sum()), len(t)-int(t.sum())],
                                                   [int(c.sum()), len(c)-int(c.sum())]]).pvalue))
    return result
