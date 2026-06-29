@echo off
cd /d "%~dp0"
(
  echo ===== PYTHON LOCATIONS =====
  echo --- where py ---
  where py
  echo --- where python ---
  where python
  echo ===== PYTHON VERSION =====
  py -3 --version
  python --version
  echo ===== INSTALL REQUIREMENTS =====
  py -3 -m pip install -r requirements.txt
  python -m pip install -r requirements.txt
  echo ===== CONNECTION CHECK =====
  py -3 run.py check
  python run.py check
  echo ===== DONE =====
) > DIAGNOSTIC.txt 2>&1
