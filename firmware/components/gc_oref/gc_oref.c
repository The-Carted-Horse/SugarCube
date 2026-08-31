/*
 * A port of glucocube/oref.py. Read the two side by side: the order of
 * operations here follows the Python statement for statement, because the
 * host test compares their outputs against the same golden vectors and a
 * "tidier" arrangement is how a forecast quietly stops matching.
 *
 * The arithmetic is double precision even though the ESP32-S3's FPU is
 * single, and even though the inputs arrive as floats. It runs once a
 * second for at most four people over 24 steps, so the software doubles
 * cost nothing that matters, and it keeps the results within a thousandth
 * of a mg/dL of the Python rather than within a tenth.
 */

#include "gc_oref.h"

#include <math.h>
#include <stddef.h>

typedef struct {
    double age_min;
    double units;
} bolus_t;

/* One synthetic bolus, or every real one inside the insulin duration. */
#define MAX_KNOWN GC_MAX_TREATMENTS

/* The peak has to sit before the midpoint of the duration: the tau term
 * below divides by zero at exactly peak == duration / 2, and a profile
 * saying "3-hour DIA, 90-minute peak" is that case. Same clamp, same
 * constant, as oref.insulin_model on the Pi. */
static double clamp_peak(double td_min, double tp_min)
{
    double ceiling = td_min * GC_PEAK_MAX_FRACTION_OF_DIA;
    return tp_min > ceiling ? ceiling : tp_min;
}

static double plausible(bool has, float value, double lo, double hi, double fallback)
{
    /* Python: `float(value) if value and lo <= value <= hi else None`.
     * The bare `value` rejects zero, which is what a profile writes when it
     * means "I do not know". */
    if (!has || value == 0.0f) {
        return fallback;
    }
    double v = (double)value;
    return (v >= lo && v <= hi) ? v : fallback;
}

gc_therapy_t gc_therapy_from_params(const gc_params_t *params)
{
    gc_therapy_t t = {
        .isf = GC_ISF_DEFAULT,
        .cr = GC_CR_DEFAULT,
        .dia_hours = GC_DIA_HOURS_DEFAULT,
        .peak_min = GC_PEAK_MIN_DEFAULT,
    };
    if (params == NULL) {
        return t;
    }
    t.isf = (float)plausible(params->has_isf, params->isf,
                             GC_ISF_MIN, GC_ISF_MAX, GC_ISF_DEFAULT);
    t.cr = (float)plausible(params->has_cr, params->cr,
                            GC_CR_MIN, GC_CR_MAX, GC_CR_DEFAULT);
    t.dia_hours = (float)plausible(params->has_dia_hours, params->dia_hours,
                                   GC_DIA_HOURS_MIN, GC_DIA_HOURS_MAX,
                                   GC_DIA_HOURS_DEFAULT);
    t.peak_min = (float)plausible(params->has_peak_min, params->peak_min,
                                  GC_PEAK_MIN_MIN, GC_PEAK_MIN_MAX,
                                  GC_PEAK_MIN_DEFAULT);
    return t;
}

gc_insulin_model_t gc_insulin_model(float td_min, float tp_min)
{
    double td = td_min;
    double tp = clamp_peak(td, tp_min);
    double tau = tp * (1.0 - tp / td) / (1.0 - 2.0 * tp / td);
    double a = 2.0 * tau / td;
    double s = 1.0 / (1.0 - a + (1.0 + a) * exp(-td / tau));
    gc_insulin_model_t model = {
        .td_min = td_min,
        .tp_min = tp_min,
        .tau = (float)tau,
        .a = (float)a,
        .s = (float)s,
    };
    return model;
}

/* The model's own doubles, recomputed rather than read back from the float
 * fields above, so the curve is evaluated at full precision. */
typedef struct {
    double td;
    double tau;
    double a;
    double s;
} model_d_t;

static model_d_t model_double(double td_min, double tp_min)
{
    double tp = clamp_peak(td_min, tp_min);
    double tau = tp * (1.0 - tp / td_min) / (1.0 - 2.0 * tp / td_min);
    double a = 2.0 * tau / td_min;
    double s = 1.0 / (1.0 - a + (1.0 + a) * exp(-td_min / tau));
    model_d_t m = {.td = td_min, .tau = tau, .a = a, .s = s};
    return m;
}

static double activity_d(const model_d_t *m, double t, double u)
{
    if (t <= 0.0 || t >= m->td) {
        return 0.0;
    }
    return u * (m->s / (m->tau * m->tau)) * t * (1.0 - t / m->td) * exp(-t / m->tau);
}

static double iob_frac_d(const model_d_t *m, double t)
{
    if (t <= 0.0) {
        return 1.0;
    }
    if (t >= m->td) {
        return 0.0;
    }
    return 1.0 - m->s * (1.0 - m->a)
                     * ((t * t / (m->tau * m->td * (1.0 - m->a)) - t / m->tau - 1.0)
                            * exp(-t / m->tau)
                        + 1.0);
}

float gc_insulin_activity(const gc_insulin_model_t *model, float t_min, float units)
{
    model_d_t m = model_double(model->td_min, model->tp_min);
    return (float)activity_d(&m, t_min, units);
}

float gc_insulin_iob_frac(const gc_insulin_model_t *model, float t_min)
{
    model_d_t m = model_double(model->td_min, model->tp_min);
    return (float)iob_frac_d(&m, t_min);
}

/* scale * sum over known boluses of activity at (age + minutes_ahead). */
static double activity_at(const model_d_t *m, const bolus_t *known, int count,
                          double scale, double minutes_ahead)
{
    double total = 0.0;
    for (int i = 0; i < count; i++) {
        total += activity_d(m, known[i].age_min + minutes_ahead, known[i].units);
    }
    return scale * total;
}

static double clampd(double v, double lo, double hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

int gc_oref_predict(const gc_oref_input_t *input, int steps,
                    float *out_values, gc_curve_t *out_curve)
{
    if (input == NULL || out_values == NULL || steps <= 0
        || steps > GC_OREF_MAX_STEPS) {
        return 0;
    }

    gc_therapy_t therapy = gc_therapy_from_params(input->params);
    double td = (double)therapy.dia_hours * 60.0;
    double isf = therapy.isf;
    model_d_t model = model_double(td, therapy.peak_min);

    /* --- effective bolus list, rescaled to the pump's reported IOB --- */
    bolus_t known[MAX_KNOWN];
    int known_count = 0;
    for (int i = 0; i < input->bolus_count && known_count < MAX_KNOWN; i++) {
        double age = (double)(input->now_ms - input->boluses[i].ms) / 60000.0;
        double units = input->boluses[i].value;
        if (age >= 0.0 && age < td && units > 0.0) {
            known[known_count].age_min = age;
            known[known_count].units = units;
            known_count++;
        }
    }

    double computed_iob = 0.0;
    for (int i = 0; i < known_count; i++) {
        computed_iob += known[i].units * iob_frac_d(&model, known[i].age_min);
    }

    double scale = 1.0;
    if (input->has_pump_iob) {
        if (computed_iob > 0.1) {
            scale = clampd((double)input->pump_iob / computed_iob,
                           GC_IOB_SCALE_MIN, GC_IOB_SCALE_MAX);
        } else {
            /* No visible boluses: model the reported IOB as one synthetic
             * bolus about an hour old (mid-decay). Works for negative IOB
             * too, which then correctly pushes predictions upward. */
            double frac = iob_frac_d(&model, GC_SYNTHETIC_BOLUS_AGE_MIN);
            if (frac < GC_SYNTHETIC_BOLUS_MIN_FRAC) {
                frac = GC_SYNTHETIC_BOLUS_MIN_FRAC;
            }
            known[0].age_min = GC_SYNTHETIC_BOLUS_AGE_MIN;
            known[0].units = (double)input->pump_iob / frac;
            known_count = 1;
        }
    }

    /* --- deviations: actual BG movement minus insulin-explained movement --- */
    double dev_total = 0.0;
    int dev_count = 0;
    const gc_point_t *history = input->history;
    int n = input->history_count;
    for (int i = 0; i + 1 < n; i++) {
        /* Python filters the history to the deviation window first, then
         * walks consecutive pairs of what is left; a pair is only formed
         * when both of its readings survived that filter. */
        if (input->now_ms - history[i].ms > GC_DEVIATION_WINDOW_MS
            || input->now_ms - history[i + 1].ms > GC_DEVIATION_WINDOW_MS) {
            continue;
        }
        double t0 = (double)history[i].ms, v0 = history[i].value;
        double t1 = (double)history[i + 1].ms, v1 = history[i + 1].value;
        double gap_min = (t1 - t0) / 60000.0;
        if (gap_min < GC_DEVIATION_MIN_GAP_MIN || gap_min > GC_DEVIATION_MAX_GAP_MIN) {
            continue;
        }
        double age_mid = ((double)input->now_ms - (t0 + t1) / 2.0) / 60000.0;
        double expected = 0.0;
        for (int k = 0; k < known_count; k++) {
            expected += activity_d(&model, known[k].age_min - age_mid, known[k].units);
        }
        expected = -scale * expected * isf * gap_min;
        double actual = v1 - v0;
        dev_total += (actual - expected) * (GC_STEP_MIN / gap_min);
        dev_count++;
    }
    double avg_dev = dev_count > 0 ? dev_total / dev_count : 0.0;

    /* --- carb impact (oref: deviation-driven, floored while COB remains) --- */
    double cob = input->has_cob ? (double)input->cob : 0.0;
    if (cob < 0.0) {
        cob = 0.0;
    }
    double csf = isf / (double)therapy.cr;          /* mg/dL per gram */
    double ci = avg_dev;
    double floor_ci = cob > 0.0 ? GC_MIN_5M_CARBIMPACT : 0.0;
    if (ci < floor_ci) {
        ci = floor_ci;
    }
    /* Linear decay duration so the area under predCI equals COB * CSF. */
    double cob_steps = (cob > 0.0 && ci > 0.0)
                           ? (2.0 * cob * csf / ci) / GC_STEP_MIN
                           : 0.0;

    double sgv = input->sgv;
    double iob_pred = sgv, cob_pred = sgv, uam_pred = sgv;
    double iob_out[GC_OREF_MAX_STEPS], cob_out[GC_OREF_MAX_STEPS];
    double uam_out[GC_OREF_MAX_STEPS];

    for (int i = 0; i < steps; i++) {
        double bgi = -activity_at(&model, known, known_count, scale,
                                  ((double)i + 0.5) * GC_STEP_MIN)
                     * isf * GC_STEP_MIN;
        double pred_ci = 0.0;
        if (cob_steps > 0.0) {
            double decay = 1.0 - (double)i / cob_steps;
            pred_ci = ci * (decay > 0.0 ? decay : 0.0);
        }
        double uam_decay = 1.0 - (double)i / (double)GC_UAM_DECAY_STEPS;
        double uam_ci = avg_dev * (uam_decay > 0.0 ? uam_decay : 0.0);

        iob_pred = clampd(iob_pred + bgi, GC_CLAMP_LO, GC_CLAMP_HI);
        cob_pred = clampd(cob_pred + bgi + pred_ci, GC_CLAMP_LO, GC_CLAMP_HI);
        uam_pred = clampd(uam_pred + bgi + uam_ci, GC_CLAMP_LO, GC_CLAMP_HI);
        iob_out[i] = iob_pred;
        cob_out[i] = cob_pred;
        uam_out[i] = uam_pred;
    }

    const double *chosen;
    gc_curve_t curve;
    if (cob > 0.0) {
        chosen = cob_out;
        curve = GC_CURVE_COB;
    } else if (fabs(avg_dev) > GC_UAM_DEVIATION_THRESHOLD) {
        chosen = uam_out;
        curve = GC_CURVE_UAM;
    } else {
        chosen = iob_out;
        curve = GC_CURVE_IOB;
    }
    for (int i = 0; i < steps; i++) {
        out_values[i] = (float)chosen[i];
    }
    if (out_curve != NULL) {
        *out_curve = curve;
    }
    return steps;
}
