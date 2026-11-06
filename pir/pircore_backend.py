"""Adapter: plug the team's pir_core backends (XorPIR / LwePIR, Member A's package) into the
seekable player / playback evaluation, which expect backend.retrieve(indices) -> list[bytes].

    from pircore_backend import PirCoreBackend
    from pir_core import LwePIR
    backend = PirCoreBackend(LwePIR(), recs)      # recs: (N, R) uint8 from pir_source.load_catalog_records
    src = pir_source.PIRChunkSource(manifest, index, backend)
"""
import numpy as np

import pir_source as ps


class PirCoreBackend:
    def __init__(self, core_backend, records, link=None):
        self.core = core_backend
        self.name = "pir_core:" + type(core_backend).__name__
        self.link = link
        self.stats = ps._Stats()
        core_backend.setup(np.ascontiguousarray(records, dtype=np.uint8))
        srv = getattr(core_backend, "server", None)   # LwePIR keeps its server (and hint) after setup()
        hint = srv.hint().nbytes if srv is not None and hasattr(srv, "hint") else 0
        self.offline = {"hint_bytes": int(hint),
                        "setup_s": round(getattr(core_backend, "offline_server_s", 0.0)
                                         + getattr(core_backend, "offline_client_s", 0.0), 3)}

    def retrieve(self, indices):
        out = self.core.retrieve(list(indices))
        st = self.core.last_stats
        s = self.stats
        n = len(indices)
        up, down = int(st.bytes_up), int(st.bytes_down)
        s.rounds += 1
        s.records += n
        s.up_bytes += up
        s.down_bytes += down
        s.server_s += float(st.t_server_max)
        s.client_s += float(st.t_query) + float(st.t_decode)
        s.wire_s += ps._wire(self.link, up, down)
        return [out[j].tobytes() for j in range(n)]
