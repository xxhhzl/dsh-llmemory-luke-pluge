# -*- coding: utf-8 -*-
"""事件切分实验脚手架（Step 1）。

只做一件事：把一段消息序列切成「线性、不重叠、不留空」的事件序列，
并给出每个边界判定的来源（rule / jev / escalated / fallback）。

实现已迁到 src/：判定客户端 src/jev.py，切分器 src/segmenter.py；
本包只保留离线的数据读取（dsh_sessions）、脚手架脚本与测试辅助。
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
