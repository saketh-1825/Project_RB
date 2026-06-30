import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from workflow.graph import builder

print("nodes:", list(builder.nodes.keys()))
