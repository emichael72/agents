# Gatekeepers

The rules that hold the agents, and the code they write, to account. Unlike the tools, the agents do
not call these directly: they decide what a tool may touch, and what may merge.

| Gatekeeper            | Guards          | How                                                                                                                                                                                                                                    |
|-----------------------|-----------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`fs`](fs/fs_gate.py) | The file system | Every tool that takes a path (`shell`, `ed`, `doxy`, `pr`) checks it against the folders and access rights (`r`, `w`, `x`) in [`context/paths.json`](../context/paths.json); `shell` also mounts exactly those folders in its sandbox. |
| [`pr`](pr/README.md)  | Pull requests   | A service that builds and tests each revision of a pull request, checks its documentation, and quizzes its author before GitHub lets it merge (the `developer-quiz` check).                                                            |

The agents see the pull request gate through the [`pr_gate`](../tools/pr_gate/README.md) tool, which
reports each open pull request's state. Its service is managed with `./install.sh --gate ...`.

The model cannot change either gatekeeper: `ed` refuses the `gatekeepers`, `tools` and `context`
folders, and the shell's sandbox does not contain them (or only read-only).
