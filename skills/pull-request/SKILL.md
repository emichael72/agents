---
name: pull-request
description: Change code in a repository (a feature, a fix, a new module or option) and open a pull request (merge request, MR) for it.
---

# Change code and open a pull request

The user may call a pull request a merge request or MR: it is the same thing, and the pr tool opens
it. Follow these steps in order.

1. Sync: run the pr tool with action sync, so you work on the latest code from GitHub (the shell
   has no network).
2. Understand: read the files involved with cat -n. Use grep to find every place the change must
   go; a new command-line option, for example, is usually listed in the help text, the options
   table and the short options string. Missing one of them is the most common bug.
3. Edit with ed, never with the shell: ed creates files (action write) and changes them. Prefer
   replace, with old copied exactly from the file; keep each edit small, and look at the lines ed
   shows afterwards. Document every new file and function with Doxygen comments, like the existing
   code: each function once, in its header (@brief, @param, @return), and the .c file only a @file
   block. Documenting a function in both places makes doxy report every parameter twice.
4. Test what you added: add commands for every new option or behavior, including one that must
   fail (written as ! command), to the check target of the Makefile.
5. Check with the pr tool, action check: it runs the reviewer's gate's own checks in one call (make
   with no errors and no compiler warnings, make check, and doxy on the changed files). Fix
   whatever it reports and run it again until it passes.
6. Open the pull request with the pr tool, action open (it runs the same checks and refuses a
   change that fails them): a title saying what the change does, and a body saying what changed,
   why, and which checks passed. Describe only what you did and verified. The pr tool formats,
   commits, creates the branch, pushes, and returns the pull request and quiz links: give the user
   those links, and stop.
