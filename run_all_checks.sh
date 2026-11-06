#!/usr/bin/env bash
# One command to verify the whole merged project (about 1-2 min). Needs python3, numpy, pandas, matplotlib.
set -e
cd "$(dirname "$0")"
export PYTHONPATH="$PWD"
echo "== [A] pir_core package tests (Member A, shared by network + eval)"
python3 -m unittest discover -s pir_core_extras/tests 2>&1 | tail -3
echo "== [A/B] my PIR core + seekable player tests"
(cd pir && python3 test_pir.py 2>&1 | tail -3 && python3 test_player.py 2>&1 | tail -3)
echo "== [A+B] integration: pir_core backends drive the seekable player"
python3 -m unittest discover -s pir -p "test_pircore*.py" 2>&1 | tail -3
echo "== [D] evaluation & security tests"
(cd security_eval && PYTHONPATH="$PWD:$PYTHONPATH" python3 -m unittest eval.test_eval 2>&1 | tail -3)
echo "== [C] network loopback (plaintext lab mode, then TLS 1.3 + mTLS + signed manifest)"
python3 network/loopback_check.py
echo "== [B+C] player over real TCP servers, unsharded + sharded, adaptive + fixed batching"
python3 network/test_network_stream.py 2>&1 | tail -3
echo "== [C/D] security: TLS 1.3 / mutual TLS, signed manifest, tampered-answer detection"
python3 -W ignore network/test_security.py 2>&1 | grep -E "^(Ran|OK|FAIL)"
echo "== [C/D] fault tolerance: cheating / refusing / hanging / crashed servers (XOR + LWE)"
python3 -W ignore network/test_robust.py 2>&1 | grep -E "^(Ran|OK|FAIL)"
echo "== [demo] live demo runs end to end (auto mode)"
python3 -W ignore demo/live_demo.py --auto --pause 0 > /tmp/_demo_out.txt 2>&1 && grep -c "play order correct: True" /tmp/_demo_out.txt && grep -q "Done." /tmp/_demo_out.txt
echo "ALL CHECKS PASSED"
