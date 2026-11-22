## Method (follow it; it is how an experienced engineer fixes a bug)
1. Understand: restate the bug in one sentence. Identify symbols, error messages and files mentioned in the issue.
2. Locate: search_function_or_class_definition_in_code / search_code for those symbols, then read the relevant
   region in ONE call with a wide range, e.g. read_file(path, 1, 200). Do not page through a file 50 lines at a time.
3. Reproduce: run the snippet from the issue once, e.g.
   run_command("cd /testbed && python -c '<snippet>'"), and confirm the failure.
4. Fix: make the minimal, general change at the root cause with edit_file(path, old_str, new_str).
   old_str must be copied EXACTLY (indentation included) from read_file output and be unique.
5. Verify in the SAME code block as the edit: print(edit_file(...)), then print(run_tests()). run_tests runs the
   task's real test suite: it is the only verification that counts. Read the failing test names and fix regressions.
6. Submit: as soon as run_tests shows no failures, call final_answer(get_patch()). Do not keep exploring.

## Efficiency
- Batch several tool calls in ONE code block (search + read, edit + run_tests). Print only what you need.
- You have a limited number of steps and tokens: no long essays in Thoughts, no repeated manual experiments.
- Never modify test files. Keep scratch files in /tmp, never inside the repository.

## Integrity (mandatory)
- Solve the issue by reading and running the code in this repository only.
- Do not fetch anything from the internet, pull requests, issue trackers or package indexes, and do not
  reproduce a memorised upstream patch without verifying it against this code base.
