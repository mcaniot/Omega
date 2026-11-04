#!/usr/bin/python3
import os, sys
os.execv('/home/mcaniot/.agents/tools/robot-exec/native/run', ['/home/mcaniot/.agents/tools/robot-exec/native/run', *sys.argv[1:]])
