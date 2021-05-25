from vhsdecode.addons.vsyncserration import VsyncSerration
import numpy as np
import vhsdecode.utils as utils
import lddecode.utils as lddu
import itertools
from lddecode.utils import inrange
import lddecode.core as ldd
import math
import hashlib


# stores the last valid blacklevel, synclevel and vsynclocs state
# preliminary solution to fix spurious decoding halts (numpy error case)
class FieldState:
    def __init__(self):
        self.blanklevels = utils.StackableMA()
        self.synclevels = utils.StackableMA()
        self.locs = None

    def setSyncLevel(self, level):
        self.synclevels.push(level)

    def setLevels(self, blank, sync):
        self.blanklevels.push(blank)
        self.setSyncLevel(sync)

    def getSyncLevel(self):
        return self.synclevels.pull()

    def getLevels(self):
        blevels = self.blanklevels.pull()
        if blevels is not None:
            return blevels, self.getSyncLevel()
        else:
            return None, None

    def setLocs(self, locs):
        self.locs = locs

    def getLocs(self):
        return self.locs

    def hasLevels(self):
        return (
            self.blanklevels.has_values()
            and self.synclevels.has_values()
        )


class Resync:
    def __init__(self, fs, sysparams, debug=False):
        self.debug = debug
        self.samp_rate = fs
        self.SysParams = sysparams.copy()
        self.VsyncSerration = VsyncSerration(fs, sysparams)
        self.field_state = FieldState()

    def debug_field(self, sync_reference):
        ldd.logger.debug("Hashed field sync reference %s" % hashlib.md5(sync_reference.tobytes('C')).hexdigest())

    # checks for SysParams consistency
    def sysparams_consistency_checks(self, field):
        reclamp_ire0 = False
        # AGC is allowed to change two sysparams
        if field.rf.useAGC:
            if field.rf.SysParams["ire0"] != self.SysParams["ire0"]:
                ldd.logger.debug("AGC changed SysParams[ire0]: %.02f Hz", field.rf.SysParams["ire0"])
                self.SysParams["ire0"] = field.rf.SysParams["ire0"].copy()
                reclamp_ire0 = True

            if field.rf.SysParams["hz_ire"] != self.SysParams["hz_ire"]:
                ldd.logger.debug("AGC changed SysParams[hz_ire]: %.02f Hz", field.rf.SysParams["hz_ire"])
                self.SysParams["hz_ire"] = field.rf.SysParams["hz_ire"].copy()

        if self.SysParams != field.rf.SysParams:
            ldd.logger.error('SysParams changed during runtime!')
            ldd.logger.debug('Original: %s' % self.SysParams)
            ldd.logger.debug('Altered : %s' % field.rf.SysParams)
            assert False, "SysParams changed during runtime!"

        return reclamp_ire0

    def fallback_vsync_loc_means(self, field, pulses):
        # determine sync pulses from vsync
        vsync_locs = []
        vsync_means = []

        for i, p in enumerate(pulses):
            if p.len > field.usectoinpx(10):
                vsync_locs.append(i)
                vsync_means.append(
                    np.mean(
                        field.data["video"]["demod_05"][
                            int(p.start + field.rf.freq) : int(
                                p.start + p.len - field.rf.freq
                            )
                        ]
                    )
                )

        return vsync_locs, vsync_means

    # search for black level
    def fallback_black_level(self, field, pulses, vsync_locs):
        # take the eq pulses before and after vsync
        r1 = range(vsync_locs[0] - 5, vsync_locs[0])
        r2 = range(vsync_locs[-1] + 1, vsync_locs[-1] + 6)

        black_means = []

        for i in itertools.chain(r1, r2):
            if i < 0 or i >= len(pulses):
                continue

            p = pulses[i]
            if inrange(p.len, field.rf.freq * 0.75, field.rf.freq * 3):
                black_means.append(
                    np.mean(
                        field.data["video"]["demod_05"][
                            int(p.start + (field.rf.freq * 5)) : int(
                                p.start + (field.rf.freq * 20)
                            )
                        ]
                    )
                )

        return black_means

    def fallback_levels(self, field, pulses):
        vsync_locs, vsync_means = self.fallback_vsync_loc_means(field, pulses)

        if len(vsync_means) == 0:
            synclevel = self.field_state.getSyncLevel()
            if synclevel is None:
                return None, None
        else:
            synclevel = np.median(vsync_means)
            self.field_state.setSyncLevel(synclevel)
            self.field_state.setLocs(vsync_locs)

        if np.abs(field.rf.hztoire(synclevel) - field.rf.SysParams["vsync_ire"]) < 5:
            # sync level is close enough to use
            return np.nan, np.nan

        if vsync_locs is None or not len(vsync_locs):
            vsync_locs = self.field_state.getLocs()

        # Now compute black level and try again
        black_means = self.fallback_black_level(field, pulses, vsync_locs)

        # Set to nan if empty to avoid warning.
        blacklevel = math.nan if len(black_means) == 0 else np.median(black_means)

        if np.isnan(blacklevel).any() or np.isnan(synclevel).any():
            # utils.plot_scope(field.data["video"]["demod_05"], title='Failed field demod05')
            bl, sl = self.field_state.getLevels()
            if bl is not None and sl is not None:
                blacklevel, synclevel = bl, sl
            else:
                return None, None
        else:
            self.field_state.setLevels(blacklevel, synclevel)

        pulse_hz_min = synclevel - (field.rf.SysParams["hz_ire"] * 10)
        pulse_hz_max = (blacklevel + synclevel) / 2

        return pulse_hz_min, pulse_hz_max

    def getpulses_override(self, field):
        """Find sync pulses in the demodulated video signal

        NOTE: TEMPORARY override until an override for the value itself is added upstream.
        """

        sync_reference = field.data["video"]["demod_05"]
        if self.debug:
            self.debug_field(sync_reference)

        # measures the serration levels if possible
        self.VsyncSerration.work(sync_reference)
        # safe clips the bottom of the sync pulses but leaves picture area unchanged
        demod_data = self.VsyncSerration.safe_sync_clip(
            sync_reference, field.data["video"]["demod"]
        )

        # if has levels, then compensate blanking bias
        if self.VsyncSerration.has_levels() or self.field_state.hasLevels():
            if self.VsyncSerration.has_levels():
                sync, blank = self.VsyncSerration.get_levels()
            else:
                blank, sync = self.field_state.getLevels()

            if not field.rf.disable_dc_offset:
                # if AGC is used, ire0 parameter floats
                if self.sysparams_consistency_checks(field):
                    field.rf.SysParams["ire0"] = blank
                dc_offset = field.rf.SysParams["ire0"] - blank
                sync_reference += dc_offset
                demod_data += dc_offset
                sync, blank = sync + dc_offset, blank + dc_offset

                # forced blank
                # field.data["video"]["demod"] = np.clip(field.data["video"]["demod"], a_min=sync, a_max=blank)

            field.data["video"]["demod_05"] = np.clip(
                sync_reference, a_min=sync, a_max=blank
            )
            field.data["video"]["demod"] = demod_data
            sync_ire, blank_ire = field.rf.hztoire(sync), field.rf.hztoire(blank)
            pulse_hz_min = field.rf.iretohz(sync_ire)
            pulse_hz_max = field.rf.iretohz((sync_ire + blank_ire) / 2)
        else:
            # pass one using standard levels (fallback sync logic)
            # pulse_hz range:  vsync_ire - 10, maximum is the 50% crossing point to sync
            pulse_hz_min = field.rf.iretohz(field.rf.SysParams["vsync_ire"] - 10)
            pulse_hz_max = field.rf.iretohz(field.rf.SysParams["vsync_ire"] / 2)

            # checks if the DC offset is abnormal before correcting it
            mean_bias = self.VsyncSerration.mean_bias()
            if (
                not field.rf.disable_dc_offset
                and not pulse_hz_min
                < mean_bias
                < field.rf.iretohz(field.rf.SysParams["vsync_ire"])
            ):
                field.data["video"]["demod_05"] = (
                    sync_reference
                    - mean_bias
                    + field.rf.iretohz(field.rf.SysParams["vsync_ire"])
                )
                field.data["video"]["demod"] = (
                    demod_data
                    - mean_bias
                    + field.rf.iretohz(field.rf.SysParams["vsync_ire"])
                )

        # utils.plot_scope(field.data["video"]["demod_05"])
        pulses = lddu.findpulses(
            field.data["video"]["demod_05"], pulse_hz_min, pulse_hz_max
        )

        if len(pulses) == 0:
            # can't do anything about this
            return pulses

        # calls the fallback auto-levels
        if not self.VsyncSerration.has_levels():
            f_pulse_hz_min, f_pulse_hz_max = self.fallback_levels(field, pulses)
            if f_pulse_hz_min is None or f_pulse_hz_max is None:
                return None
            if f_pulse_hz_min is np.nan or f_pulse_hz_max is np.nan:
                return pulses
            pulse_hz_min, pulse_hz_max = f_pulse_hz_min, f_pulse_hz_max

        return lddu.findpulses(
            field.data["video"]["demod_05"], pulse_hz_min, pulse_hz_max
        )
