# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(__file__).resolve().parent
p = ROOT / templates / index.html
s = p.read_text(encoding=utf-8)

def rep(old, new, label):
    global s
    if old not in s:
        raise SystemExit(fmissing
