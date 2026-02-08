
import json

tasks_data = """
[
  {
    "run_time": "13:55:00",
    "type": "daily",
    "target_day_offset": 3,
    "config": {
      "target_times": [
        "15:00",
        "16:00"
      ],
      "target_count": 2,
      "mode": "priority",
      "priority_sequences": [
        [
          "10",
          "11"
        ]
      ]
    },
    "weekly_day": 0,
    "id": 1767938084749
  }
]
"""

tasks = json.loads(tasks_data)
task = tasks[0]
config = task.get('config')

print(f"Mode: '{config.get('mode')}'")
if config.get('mode') == 'priority':
    print("Entered priority mode")
else:
    print("Entered normal mode")
    if 'candidate_places' not in config:
        print("Missing candidate_places")
