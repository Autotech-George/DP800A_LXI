"""In-memory fake VISA resource for testing the DP800A driver."""
from __future__ import annotations

import re


class FakeVisaResource:
    timeout = 5000
    read_termination = "\n"
    write_termination = "\n"

    def __init__(self) -> None:
        self.idn = "RIGOL TECHNOLOGIES,DP832A,DP8FAKE0001,01.01.00.02.05"
        self.tracking = False
        self.memory: dict[int, dict] = {}
        self.channels = {
            ch: {
                "v": 0.0,
                "i": 0.0,
                "out": False,
                "ovp": 33.0,
                "ovp_en": False,
                "ocp": 3.3,
                "ocp_en": False,
            }
            for ch in (1, 2, 3)
        }
        self.history: list[str] = []

    # ------------------------------------------------------------------
    def close(self) -> None:
        pass

    def write(self, cmd: str) -> int:
        self.history.append(cmd)
        self._dispatch_write(cmd.strip())
        return len(cmd)

    def query(self, cmd: str) -> str:
        self.history.append(cmd)
        return self._dispatch_query(cmd.strip()) + "\n"

    # ------------------------------------------------------------------
    @staticmethod
    def _ch(token: str) -> int:
        m = re.search(r"CH(\d)", token, re.I)
        if not m:
            raise ValueError(f"no channel token in {token}")
        return int(m.group(1))

    def _dispatch_write(self, cmd: str) -> None:
        u = cmd.upper()
        if u.startswith("APPL "):
            # APPL CH1,5.000,1.000
            rest = cmd[5:]
            parts = rest.split(",")
            ch = self._ch(parts[0])
            v = float(parts[1])
            i = float(parts[2])
            self.channels[ch]["v"] = v
            self.channels[ch]["i"] = i
            return
        if u.startswith("OUTP:OVP:VAL "):
            rest = cmd.split(" ", 1)[1]
            ch_tok, val = rest.split(",")
            self.channels[self._ch(ch_tok)]["ovp"] = float(val)
            return
        if u.startswith("OUTP:OCP:VAL "):
            rest = cmd.split(" ", 1)[1]
            ch_tok, val = rest.split(",")
            self.channels[self._ch(ch_tok)]["ocp"] = float(val)
            return
        if u.startswith("OUTP:OVP "):
            rest = cmd.split(" ", 1)[1]
            ch_tok, state = rest.split(",")
            self.channels[self._ch(ch_tok)]["ovp_en"] = state.strip().upper() == "ON"
            return
        if u.startswith("OUTP:OCP "):
            rest = cmd.split(" ", 1)[1]
            ch_tok, state = rest.split(",")
            self.channels[self._ch(ch_tok)]["ocp_en"] = state.strip().upper() == "ON"
            return
        if u.startswith("OUTP:TRACK "):
            self.tracking = cmd.split(" ", 1)[1].strip() in ("1", "ON")
            return
        if u.startswith("OUTP "):
            rest = cmd[5:]
            ch_tok, state = rest.split(",")
            self.channels[self._ch(ch_tok)]["out"] = state.strip().upper() == "ON"
            return
        if u.startswith("*SAV "):
            slot = int(cmd.split()[1])
            self.memory[slot] = {ch: dict(s) for ch, s in self.channels.items()}
            return
        if u.startswith("*RCL "):
            slot = int(cmd.split()[1])
            if slot in self.memory:
                for ch, s in self.memory[slot].items():
                    self.channels[ch] = dict(s)
            return
        # Unknown writes are silently accepted (real instrument may error).

    def _dispatch_query(self, cmd: str) -> str:
        u = cmd.upper()
        if u == "*IDN?":
            return self.idn
        if u.startswith("OUTP:OVP:VAL? "):
            ch = self._ch(cmd.split("? ", 1)[1])
            return f"{self.channels[ch]['ovp']:.4f}"
        if u.startswith("OUTP:OCP:VAL? "):
            ch = self._ch(cmd.split("? ", 1)[1])
            return f"{self.channels[ch]['ocp']:.4f}"
        if u.startswith("OUTP:OVP? "):
            ch = self._ch(cmd.split("? ", 1)[1])
            return "ON" if self.channels[ch]["ovp_en"] else "OFF"
        if u.startswith("OUTP:OCP? "):
            ch = self._ch(cmd.split("? ", 1)[1])
            return "ON" if self.channels[ch]["ocp_en"] else "OFF"
        if u.startswith("OUTP:TRACK?"):
            return "ON" if self.tracking else "OFF"
        if u.startswith("OUTP? "):
            ch = self._ch(cmd.split("? ", 1)[1])
            return "ON" if self.channels[ch]["out"] else "OFF"
        if u.startswith("APPL? "):
            ch = self._ch(cmd.split("? ", 1)[1])
            return f"CH{ch}:30V/3A,{self.channels[ch]['v']:.4f},{self.channels[ch]['i']:.4f}"
        if u.startswith("MEAS:VOLT? "):
            ch = self._ch(cmd.split("? ", 1)[1])
            v = self.channels[ch]["v"] if self.channels[ch]["out"] else 0.0
            return f"{v:.4f}"
        if u.startswith("MEAS:CURR? "):
            ch = self._ch(cmd.split("? ", 1)[1])
            i = self.channels[ch]["i"] if self.channels[ch]["out"] else 0.0
            return f"{i:.4f}"
        if u.startswith("MEAS:POWE? "):
            ch = self._ch(cmd.split("? ", 1)[1])
            if self.channels[ch]["out"]:
                p = self.channels[ch]["v"] * self.channels[ch]["i"]
            else:
                p = 0.0
            return f"{p:.4f}"
        return ""


def fake_opener():
    """Return an opener function compatible with DP800ADriver(opener=...)."""
    instance_holder: dict = {}

    def opener(_resource: str) -> FakeVisaResource:
        inst = FakeVisaResource()
        instance_holder["last"] = inst
        return inst

    opener.holder = instance_holder  # type: ignore[attr-defined]
    return opener
