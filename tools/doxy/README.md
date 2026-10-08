# Documentation check

`doxy` checks C/C++ files for Doxygen comments: a file block, function descriptions, parameters, and return values. It reads the sources without changing them.

From the repository root:

~~~bash
bash tools/doxy/doxy.sh -- core_dump/src
bash tools/doxy/doxy.sh -- "core_dump/src/modules/pi.c core_dump/src/include/pi.h"
~~~

Folders are searched recursively. Files must be C/C++ sources or headers and must be inside permitted paths.

The result either says everything is documented or lists problems as `file:line: message`. The output is limited to 100 problem lines.

A missing `@file` block is reported explicitly because Doxygen can otherwise ignore that file's contents. In core_dump, document a function once in its header and put the file block in its implementation.

**Exit status matters:** documentation problems are reported with exit status 0. A nonzero status means the check could not run. The PR gate reads the report to decide whether documentation passed.

Requires Doxygen. [Doxyfile.check](https://github.com/emichael72/agents/blob/a2fe18a204843563134bb1ed0d7aaf63d558691e/tools/doxy/Doxyfile.check) holds the check settings. Both the coding agents and the PR gate use this same script.
