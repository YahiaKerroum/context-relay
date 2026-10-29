"""Command-line entry points the editor extension (and you) use for inbox, rules, watchers, stats, night shift."""
import json
import os
from pathlib import Path

from . import relay
from . import smart
def run(a):
    cmd = a[0]
    sub = a[1] if len(a) > 1 else "list"

    def opt(name, default=None):
        return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default

    cwd = opt("--cwd", os.getcwd())
    if cmd == "inbox":
        if sub == "add":
            print(json.dumps(smart.inbox_add(a[2], cwd, kind=opt("--kind", "idea"), source="editor")))
        elif sub == "rm":
            smart.inbox_remove(a[2])
        else:
            print(json.dumps(smart.inbox_list(), indent=1))
    elif cmd == "rule":
        if sub == "add":
            from . import guard
            root = guard.project_root(cwd) or cwd
            print(json.dumps(smart.add_rule(a[2], root, "global" if "--global" in a else "project")))
        elif sub == "rm":
            print(json.dumps(smart.remove_rule(int(a[2]))))
        else:
            print(json.dumps(smart.load_rules(), indent=1))
    elif cmd == "watch":
        from . import watchers
        if sub == "add":
            print(json.dumps(watchers.add_log(Path(a[2]), opt("--name"), cwd)))
        elif sub == "kaggle":
            print(json.dumps(watchers.add_kaggle(a[2], cwd)))
        elif sub == "rm":
            watchers.remove(a[2])
        else:
            print(json.dumps(watchers.load(), indent=1))
    elif cmd == "stats":
        from . import stats
        print(json.dumps(stats.build()))
    elif cmd == "night":
        from . import nightshift
        return nightshift.cli(a)
    return 0
