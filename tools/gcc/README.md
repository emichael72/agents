# GCC

Compiles C sources with gcc. Sources, include folders and the output must all be inside the
allowed folders ([`../../context/paths.json`](../../context/paths.json)). Messages show paths as
`<allowed name>/...`; at most the last 60 lines are shown.

- **Without `output`** it only checks the code (`-fsyntax-only`) and reports warnings and errors.
- **With `output`** it compiles and links a program there, e.g. `core_dump/build/test`.

**Usage Example:**

```bash
python3 gcc/gcc.py "core_dump/src/modules/pi.c" --flags "-Wall -Wextra -std=c11 -Icore_dump/src/include"
python3 gcc/gcc.py "core_dump/src/main.c core_dump/src/modules/date.c core_dump/src/modules/pi.c" \
    --output core_dump/build/check --flags "-Icore_dump/src/include -lm"
```

Allowed flags: warnings (`-Wall`, `-Wextra`, `-Werror`, `-Wno-...`), optimization (`-O0`..`-O3`,
`-Os`), debug info (`-g`), the standard (`-std=c11`), `-pedantic`, defines (`-DNAME`,
`-DNAME=value`), libraries (`-lm`) and include folders (`-I<allowed folder>`). Anything that could
write elsewhere or load code (`-o`, `-fplugin`, `-B`, `-wrapper`, `@file`, specs) is refused; use
`output` for the program's path.

For a project with a Makefile, the `make` tool builds it the way the project intends.
