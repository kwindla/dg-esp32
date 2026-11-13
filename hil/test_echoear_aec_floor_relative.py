import unittest
import numpy as np
from scipy.signal import lfilter
from echoear_aec_metrics import RATE, FRAME, AFE_DELAY, activity, reduction
from echoear_aec_campaign_analysis import echo_verdict
import echoear_aec_floor_relative as fr

N = 10 * RATE
EXTENT = (2 * RATE, 8 * RATE)
FS = 32768.0**2
T = np.arange(N) / RATE
SPEAKING = ((T >= 2) & (T < 4.5)) | ((T >= 5.5) & (T < 8))


def p_dbfs(d):
    return FS * 10**(d / 10)


def speech_like(rng):
    """Two 2.5 s 'sentences' of AM-modulated coloured noise with a 1 s gap."""
    x = lfilter([1], [1, -0.9], rng.standard_normal(N))
    env = (0.6 + 0.4 * np.sin(2 * np.pi * 4 * T)) * (((T >= 2) & (T < 4.5)) | ((T >= 5.5) & (T < 8)))
    return 3000 * x / x.std() * env


def echo_path(ref):
    return lfilter([0, 0.5, 0.3, -0.2, 0.1], [1], ref)


def scene(seed=0, residual_db=-35.0, noise_rms=10.0, noise=None):
    """mic = echo + noise; AFE = residual echo + same noise, AFE delayed 1024."""
    rng = np.random.default_rng(seed)
    ref = speech_like(rng)
    echo = echo_path(ref)
    n = rng.normal(0, noise_rms, N) if noise is None else noise
    raw = echo + n
    out = np.zeros(N + AFE_DELAY)
    out[AFE_DELAY:] = 10**(residual_db / 20) * echo + n
    return raw, out, ref, rng.normal(0, 0.5, RATE)


def room_scene(*, echo_power, residual_gain, mic_bg, afe_bg, seed=1):
    """Echo scaled to ``echo_power`` over the active frames; arbitrary backgrounds.

    AFE = residual_gain * echo + afe_bg (delayed 1024); mic = echo + mic_bg.
    """
    rng = np.random.default_rng(seed)
    ref = speech_like(rng)
    q = rng.normal(0, 0.5, RATE)
    echo = echo_path(ref)
    act = np.repeat(activity(ref, q, [EXTENT]), FRAME)
    echo *= np.sqrt(echo_power / np.mean(echo[:len(act)][act]**2))
    raw = echo + mic_bg
    out = np.zeros(N + AFE_DELAY)
    out[AFE_DELAY:] = residual_gain * echo + afe_bg
    return raw, out, ref, q


def white(power, seed=2):
    return np.random.default_rng(seed).normal(0, np.sqrt(power), N)


def tone_scene(true_dbfs, absolute_dbfs, *, echo_dbfs=-20.0, white_bg=False):
    """Sol's round-2 construction: 1 kHz echo, reference active 2-8 s, constant
    400 Hz (or white) background identical in mic and delayed AFE, chosen so the
    AFE active power is ``absolute_dbfs`` and the residual echo ``true_dbfs``.
    1 kHz and 400 Hz are orthogonal within each 320-sample frame."""
    on = (T >= 2) & (T < 8)
    ref = 8000 * np.sin(2 * np.pi * 1000 * T) * on
    q = np.random.default_rng(11).normal(0, 0.5, RATE)
    echo = np.sqrt(2 * p_dbfs(echo_dbfs)) * np.sin(2 * np.pi * 1000 * T + 0.3) * on
    bg_power = p_dbfs(absolute_dbfs) - p_dbfs(true_dbfs)
    bg = (np.sqrt(bg_power) * np.random.default_rng(12).standard_normal(N) if white_bg else
          np.sqrt(2 * bg_power) * np.sin(2 * np.pi * 400 * T + 1.1))
    out = np.zeros(N + AFE_DELAY)
    out[AFE_DELAY:] = np.sqrt(p_dbfs(true_dbfs) / p_dbfs(echo_dbfs)) * echo + bg
    return echo + bg, out, ref, q


def verdict(raw, afe, ref, q, source_dbfs=0.0, **kw):
    m = fr.floor_relative_metrics(raw, afe, ref, q, EXTENT, **kw)
    return m, fr.proposed_verdict(m, source_dbfs=source_dbfs)


def add(raw, afe, n):
    afe = afe.copy()
    afe[AFE_DELAY:AFE_DELAY + len(n)] += n
    return raw + n, afe


class FloorRelativeTest(unittest.TestCase):
    def test_masks_are_the_analyzer_masks(self):
        raw, afe, ref, q = scene()
        gate = [None] * 50 + [True] * 100 + [False] * (N // FRAME - 150)
        m = fr.reply_masks(ref, q, EXTENT, gate=gate)
        np.testing.assert_array_equal(m['whole'], activity(ref, q, [EXTENT]))
        self.assertFalse((m['post_guard'] & np.array([g is not False for g in gate])).any())
        self.assertFalse((m['floor'] & activity(ref, q, [(0, len(ref))])).any())
        self.assertTrue(m['floor'].sum() > 100)

    def test_pass_is_exactly_the_frozen_verdict(self):
        for res, rms, src in ((-35, 10, 0.0), (-15, 5, 0.0), (-45, 300, 0.0), (-40, 5, -40.0), (-40, 5, None)):
            raw, afe, ref, q = scene(residual_db=res, noise_rms=rms)
            mask = activity(ref, q, [EXTENT])
            ids = np.flatnonzero(mask)
            first = mask.copy()
            first[ids[0] + 25:] = False
            frozen = echo_verdict(reduction(raw, afe, mask), reduction(raw, afe, first), src, reduction(raw, afe, mask))
            _, v = verdict(raw, afe, ref, q, source_dbfs=src)
            self.assertEqual(v['passed'], frozen['passed'], msg=(res, rms, src))
            self.assertEqual(v['frozen_passed'], frozen['passed'])
        self.assertEqual(v['verdict'], 'unscorable')  # no source level is never a pass

    def test_point_estimate_recovers_known_erle(self):
        raw, afe, ref, q = scene(residual_db=-25, noise_rms=5)
        r = fr.floor_relative_metrics(raw, afe, ref, q, EXTENT)
        self.assertTrue(r['whole']['resolved'])
        self.assertAlmostEqual(r['whole']['erle_db'], 25, delta=0.3)

    def test_floor_rise_leaves_erle_but_flips_absolute_residual(self):
        raw, afe, ref, q = scene(residual_db=-35, noise_rms=20)
        base = fr.floor_relative_metrics(raw, afe, ref, q, EXTENT)
        mask = activity(ref, q, [EXTENT])
        for kind in ('white', 'brown', 'hum'):
            rng = np.random.default_rng(7)
            if kind == 'white':
                n = rng.standard_normal(N)
            elif kind == 'brown':
                n = lfilter([1], [1, -0.995], rng.standard_normal(N))
            else:
                n = np.sin(2 * np.pi * 60 * T)
            n *= 20 * np.sqrt(10**(10 / 10) - 1) / n.std()  # +10 dB floor
            r2, a2 = add(raw, afe, n)
            noisy = fr.floor_relative_metrics(r2, a2, ref, q, EXTENT)
            self.assertAlmostEqual(noisy['whole']['erle_db'], base['whole']['erle_db'], delta=1.0, msg=kind)
            before, after = reduction(raw, afe, mask), reduction(r2, a2, mask)
            self.assertGreater(after['residual_dbfs'] - before['residual_dbfs'], 3, msg=kind)

    def test_leak_fails_and_clean_passes(self):
        _, v = verdict(*scene(residual_db=-15, noise_rms=5))
        self.assertEqual(v['verdict'], 'fail')
        self.assertIn('bounded-miss', v['carried_by'])
        _, v = verdict(*scene(residual_db=-40, noise_rms=5))
        self.assertEqual((v['verdict'], v['carried_by']), ('pass', ['absolute']))

    # --- counterexamples from the step-9 reviews: none may PASS -------------
    # (2580b74's rule passed the loud variants of all three: Sol's tone and
    # gain cases at 25000, Fable's -40/-2 dB and -35/-1 dB.)

    def test_sol_tone_background_dipping_during_playback_never_passes(self):
        # Mic echo power 1e6, residual 0.15 x echo (ERLE 16.5 dB, -46.8 dBFS):
        # a real leak. 400 Hz background at 0.4x amplitude during playback.
        tone = np.sin(2 * np.pi * 400 * T + 0.7) * np.where(SPEAKING, 0.4, 1.0)
        out = {}
        for power in (790.569, 25000.0):
            bg = np.sqrt(2 * power) * tone
            m, v = verdict(*room_scene(echo_power=1e6, residual_gain=0.15, mic_bg=bg, afe_bg=bg))
            self.assertNotEqual(v['verdict'], 'pass', msg=power)
            out[power] = v['verdict']
        self.assertEqual(out[790.569], 'fail')
        self.assertEqual(out[25000.0], 'floor-limited')

    def test_sol_constant_noise_with_active_afe_gain_never_passes(self):
        for power in (790.569, 7905.69, 25000.0):
            n = white(power)
            m, v = verdict(*room_scene(echo_power=1e6, residual_gain=0.15, mic_bg=n,
                                       afe_bg=n * np.where(SPEAKING, 0.4, 1.0)))
            self.assertNotEqual(v['verdict'], 'pass', msg=power)

    def test_fable_steady_loud_room_with_real_residual_never_passes(self):
        # True residual -44.2 dBFS; steady -40 dBFS room; AFE active noise 0..2 dB low.
        for room, cut in ((-60, 0), (-40, 0), (-40, 1), (-40, 2), (-35, 1)):
            n = white(p_dbfs(room))
            res_gain = np.sqrt(p_dbfs(-44.2) / p_dbfs(-20))
            m, v = verdict(*room_scene(echo_power=p_dbfs(-20), residual_gain=res_gain, mic_bg=n,
                                       afe_bg=n * np.where(SPEAKING, 10**(-cut / 20), 1.0)))
            self.assertNotEqual(v['verdict'], 'pass', msg=(room, cut))
            if room == -60:
                self.assertEqual(v['verdict'], 'fail')

    def test_quiet_real_leak_fails(self):
        n = white(p_dbfs(-70))
        m, v = verdict(*room_scene(echo_power=p_dbfs(-20), residual_gain=10**(-24 / 20), mic_bg=n, afe_bg=n))
        self.assertEqual(v['verdict'], 'fail')
        post = v['checks']['post_guard']
        self.assertEqual((post['outcome'], post['clause'], post['resolved']), ('fail', 'bounded-miss', True))
        self.assertAlmostEqual(post['corrected'], -44.0, delta=0.3)

    def test_room_only_absolute_miss_is_floor_limited_not_fail(self):
        n = white(p_dbfs(-45))
        m, v = verdict(*room_scene(echo_power=p_dbfs(-20), residual_gain=10**(-45 / 20), mic_bg=n, afe_bg=n))
        self.assertGreater(v['checks']['post_guard']['absolute'], -50)  # the frozen gate fails it
        self.assertFalse(v['frozen_passed'])
        self.assertEqual(v['verdict'], 'floor-limited')
        self.assertNotIn('fail', [c['outcome'] for c in v['checks'].values()])

    def test_floor_drift_disables_correction(self):
        # Leak in a quiet but drifting room: the room cannot explain the miss, still FAIL.
        n = np.random.default_rng(4).normal(0, 1, N) * np.sqrt(np.where(T < 5, p_dbfs(-80), p_dbfs(-70)))
        m, v = verdict(*room_scene(echo_power=p_dbfs(-20), residual_gain=10**(-24 / 20), mic_bg=n, afe_bg=n))
        self.assertFalse(m['floor']['halves']['stable'])
        self.assertFalse(m['post_guard']['scorable'])
        self.assertEqual((v['verdict'], v['checks']['post_guard']['clause']), ('fail', 'bounded-miss'))
        # Clean echo in a loud drifting room: floor-limited.
        n = np.random.default_rng(4).normal(0, 1, N) * np.sqrt(np.where(T < 5, p_dbfs(-50), p_dbfs(-44)))
        m, v = verdict(*room_scene(echo_power=p_dbfs(-20), residual_gain=10**(-45 / 20), mic_bg=n, afe_bg=n))
        self.assertEqual(v['verdict'], 'floor-limited')

    # --- review round 2 (Sol): threshold-near misses the room explains -------

    def test_sol_threshold_near_room_explained_misses_are_floor_limited(self):
        # Sol's table: the room contribution (0.30/0.06/0.45 dB, all < 0.5 dB)
        # fully explains the post-guard miss; 2580b74..096e0a1 returned FAIL.
        for true, absolute in ((-50.20, -49.90), (-50.05, -49.99), (-50.40, -49.95)):
            m, v = verdict(*tone_scene(true, absolute))
            post = v['checks']['post_guard']
            self.assertAlmostEqual(post['absolute'], absolute, delta=0.005, msg=true)
            self.assertLess(post['room_share_db'], 0.5)
            self.assertLessEqual(post['favourable'], -50.0)
            self.assertEqual((v['verdict'], post['outcome']), ('floor-limited', 'floor-limited'), msg=true)

    def test_sweep_room_explained_misses_never_fail(self):
        for true in (-50.45, -50.30, -50.15, -50.02):
            for share in (0.03, 0.1, 0.2, 0.3, 0.4, 0.49):
                absolute = true + share
                if absolute <= -50.0:
                    continue
                for kind in ('tone', 'white'):
                    m, v = verdict(*tone_scene(true, absolute, white_bg=kind == 'white'))
                    self.assertFalse(v['frozen_passed'])
                    self.assertEqual(v['verdict'], 'floor-limited', msg=(true, share, kind))

    def test_threshold_near_erle_miss_the_room_explains_is_floor_limited(self):
        # True ERLE 20.2 dB, room-raised absolute 19.8 dB; post-guard also room-raised.
        m, v = verdict(*tone_scene(-50.1, -49.68, echo_dbfs=-29.9))
        whole = v['checks']['whole']
        self.assertLess(whole['absolute'], 20.0)
        self.assertGreaterEqual(whole['favourable'], 20.0)
        self.assertEqual((whole['outcome'], v['verdict']), ('floor-limited', 'floor-limited'))

    def test_threshold_near_real_miss_still_fails(self):
        # True residual -49.7 dBFS misses by itself; the 0.1 dB room cannot rescue it.
        for kind in (False, True):
            m, v = verdict(*tone_scene(-49.7, -49.6, white_bg=kind))
            post = v['checks']['post_guard']
            self.assertEqual((v['verdict'], post['clause']), ('fail', 'bounded-miss'), msg=kind)
            self.assertGreater(post['favourable'], -50.0)
        # ERLE: true 19.6 dB with a small room share still fails on whole.
        m, v = verdict(*tone_scene(-49.6 - 0.0, -49.5, echo_dbfs=-30.0))
        self.assertEqual(v['checks']['whole']['outcome'], 'fail')

    def test_below_floor_is_never_certified(self):
        # AFE active power below its silent floor: not proof of zero echo.
        n = white(p_dbfs(-45))
        m, v = verdict(*room_scene(echo_power=p_dbfs(-20), residual_gain=10**(-28 / 20), mic_bg=n,
                                   afe_bg=n * np.where(SPEAKING, 0.1, 1.0)))
        self.assertTrue(m['post_guard']['below_floor'])
        self.assertFalse(m['post_guard']['scorable'])
        self.assertNotEqual(v['verdict'], 'pass')
        self.assertEqual(v['verdict'], 'floor-limited')

    def test_floor_limited_is_never_a_pass(self):
        # Residual below a loud, slowly fluctuating room: cannot certify.
        rng = np.random.default_rng(3)
        n = rng.normal(0, 1, N) * 300 * (1 + 0.8 * np.sin(2 * np.pi * 0.37 * T))
        _, v = verdict(*scene(residual_db=-45, noise=n))
        self.assertEqual(v['verdict'], 'floor-limited')

    def test_no_floor_or_no_echo(self):
        raw, afe, ref, q = scene()
        params = dict(fr.PARAMS, min_floor_blocks=N // FRAME)
        m, v = verdict(raw, afe, ref, q, params=params)
        self.assertFalse(m['floor']['scorable'])
        self.assertEqual(v['verdict'], 'pass')  # absolute clause needs no floor
        m, v = verdict(*scene(residual_db=-15), params=params)
        self.assertEqual(v['verdict'], 'floor-limited')  # no floor: cannot bound the room
        m, v = verdict(*scene(noise_rms=3000))  # echo not above the room
        self.assertFalse(m['whole']['scorable'])
        self.assertEqual(v['verdict'], 'floor-limited')

    def test_exact_zero_floor(self):
        raw, afe, ref, q = scene(noise=np.zeros(N))
        m, v = verdict(raw, afe, ref, q)
        self.assertEqual(m['floor']['afe_power'], 0.0)
        self.assertEqual(m['post_guard']['excess_db'], float('inf'))
        self.assertEqual(v['verdict'], 'pass')
        m, v = verdict(*scene(residual_db=-15, noise=np.zeros(N)))
        self.assertEqual(v['verdict'], 'fail')

    def test_floor_blocks_stay_inside_contiguous_runs(self):
        silent = np.zeros(400, dtype=bool)
        for s in (10, 110, 210):
            silent[s:s + 40] = True
        p = np.where(np.arange(400) < 150, 1.0, 100.0)
        f = fr.floor_estimate(p, p, silent, np.array([300]))
        self.assertEqual(f['blocks'], 3)  # compaction would cut 4 blocks from 120 frames
        for k in fr.contiguous_blocks(np.flatnonzero(silent), 25):
            self.assertTrue((np.diff(k) == 1).all())

    def test_short_window_variance(self):
        self.assertEqual(fr.mean_sigma(1, 10.0, 2.0, 25), 10.0)
        self.assertAlmostEqual(fr.mean_sigma(5, 10.0, 2.0, 25), 10 / np.sqrt(5))  # independent frames
        self.assertAlmostEqual(fr.mean_sigma(5, 10.0, 8.0, 25), 10.0)  # correlated: capped at one frame
        self.assertAlmostEqual(fr.mean_sigma(100, 10.0, 4.0, 25), 4.0)  # no credit beyond one block
        raw, afe, ref, q = scene()
        mic_p, afe_p = fr.aligned_frame_powers(raw, afe)
        m = fr.reply_masks(ref, q, EXTENT, n_frames=len(mic_p))
        fl = fr.floor_estimate(mic_p, afe_p, m['floor'], np.flatnonzero(m['whole']))
        few = np.zeros_like(m['whole'])
        few[np.flatnonzero(m['whole'])[:3]] = True
        c = fr.floor_corrected(mic_p, afe_p, few, fl)
        self.assertFalse(c['scorable'])
        self.assertIn('Too few active frames', c['reason'])

    def test_coherence_segments_are_full_and_non_overlapping(self):
        mask = np.ones(100, dtype=bool)
        mask[50] = False
        starts = fr.coherence_starts(mask, 1024, 10**6, 10**6, AFE_DELAY)
        self.assertTrue((np.diff(starts) >= 1024).all())
        for s in starts:
            self.assertTrue(mask[s // FRAME:-(-(s + 1024) // FRAME)].all())

    def test_coherent_tracks_linear_leak_and_ignores_noise(self):
        raw, afe, ref, q = scene(residual_db=-20, noise_rms=5)
        base = fr.coherent(raw, afe, ref, activity(ref, q, [EXTENT]))
        self.assertAlmostEqual(base['erle_db'], 20, delta=1.0)
        thumps = np.random.default_rng(5).normal(0, 60, N) * (1 + 3 * np.exp(-((T * 1.1) % 1) / 0.08))
        r2, a2 = add(raw, afe, thumps)
        noisy = fr.coherent(r2, a2, ref, activity(ref, q, [EXTENT]))
        self.assertAlmostEqual(noisy['erle_db'], base['erle_db'], delta=1.5)

    def test_burst_catches_tonal_overshoot_not_noise(self):
        raw, afe, ref, q = scene(residual_db=-35, noise_rms=5)
        clean = fr.floor_relative_metrics(raw, afe, ref, q, EXTENT)['burst']
        self.assertGreater(clean['min_local_erle_db'], 20)
        a2 = afe.copy()
        at = int(3.0 * RATE)
        a2[AFE_DELAY + at:AFE_DELAY + at + 640] += 2500 * np.sin(2 * np.pi * 900 * np.arange(640) / RATE)
        hit = fr.floor_relative_metrics(raw, a2, ref, q, EXTENT)['burst']
        self.assertLess(hit['min_local_erle_db'], 10)
        self.assertAlmostEqual(hit['at_s'], 3.0, delta=0.06)
        r3, a3 = add(raw, afe, np.random.default_rng(9).normal(0, 50, N))
        noisy = fr.floor_relative_metrics(r3, a3, ref, q, EXTENT)['burst']['min_local_erle_db']
        self.assertTrue(noisy is None or noisy > 20)

    def test_delay_is_validated(self):
        raw, afe, ref, q = scene()
        with self.assertRaises(ValueError):
            fr.aligned_frame_powers(raw, afe, delay=1024.0)


if __name__ == '__main__':
    unittest.main()
