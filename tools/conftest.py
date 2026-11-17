"""Tests read Ω-run's JSON result; a clean PASS prints plain output unless OMEGA_RUN_JSON=1.

Tests of the plain form (test_run.PlainPassTests) remove the variable from the child's environment.
"""
import os

os.environ['OMEGA_RUN_JSON'] = '1'
