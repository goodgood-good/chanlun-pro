"""Read the current A-share industry and concept membership catalog."""

from __future__ import annotations

import json
import pathlib
from typing import Dict, List, Union

from chanlun.config import get_data_path


class StocksBKGN:
    """Expose the packaged catalog, overridden by an existing data-file catalog."""

    def __init__(self) -> None:
        data_file = get_data_path() / "json" / "new_stocks_bkgn.json"
        packaged_file = pathlib.Path(__file__).parent / "new_stocks_bkgn.json"
        self.file_name = data_file if data_file.is_file() else packaged_file
        self.cache_file_bk: dict | None = None

    def file_bkgns(self) -> Dict[str, Union[Dict[str, list], List[str]]]:
        if self.cache_file_bk is None:
            if self.file_name.is_file():
                with open(self.file_name, "r", encoding="utf-8") as fp:
                    self.cache_file_bk = json.load(fp)
            else:
                self.cache_file_bk = {
                    "hys": [],
                    "gns": [],
                    "hy_codes": {},
                    "gn_codes": {},
                }
        return self.cache_file_bk

    def get_code_bkgn(self, code: str):
        """Return the industries and concepts containing one stock code."""
        code = (
            code.replace("SZ.", "")
            .replace("SH.", "")
            .replace("SZSE.", "")
            .replace("SHSE.", "")
            .replace("BJ.", "")
            .replace("BJSE.", "")
            .replace(".SZ", "")
            .replace(".SH", "")
            .replace(".BJ", "")
        )
        bkgn_infos = self.file_bkgns()
        code_hys = [
            name for name, codes in bkgn_infos["hy_codes"].items() if code in codes
        ]
        code_gns = [
            name for name, codes in bkgn_infos["gn_codes"].items() if code in codes
        ]
        return {"HY": code_hys, "GN": code_gns}

    def get_codes_by_hy(self, hy_name: str) -> List[str]:
        """Return the stock codes belonging to an industry."""
        return self.file_bkgns()["hy_codes"].get(hy_name, [])

    def get_codes_by_gn(self, gn_name: str) -> List[str]:
        """Return the stock codes belonging to a concept."""
        return self.file_bkgns()["gn_codes"].get(gn_name, [])

    @staticmethod
    def ths_to_tdx_codes(codes):
        """Convert six-digit stock codes to the exchange-prefixed form."""
        result = []
        for code in codes:
            if code.startswith("688"):
                result.append(f"SH.{code}")
            elif code[0] in {"4", "8", "9"}:
                result.append(f"BJ.{code}")
            elif code.startswith("6"):
                result.append(f"SH.{code}")
            else:
                result.append(f"SZ.{code}")
        return result
