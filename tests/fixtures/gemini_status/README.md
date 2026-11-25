Replies of `agy models </dev/null`, the doctor's Gemini status probe, run with
every Gemini auth override and `GEMINI_CLI_HOME` unset. Each reply's stdout is
in `<name>.out`, its stderr in `<name>.err` and its exit code in `<name>.code`.
A missing stream is empty. The replies come from the #1639 live check of
Antigravity `agy` 1.5.15:

- `signed-in`: the device's `gemini-bot` login is signed in, so `agy models`
  exits 0 and lists the models on stdout. The check recorded only that shape,
  so these model names were written from it and not captured. A later live
  capture can replace them.
- `signed-out`: no login, exit 1 with the CLI's exact sign-in error.
- `no-models`: exit 0 with no output, which the doctor does not read as signed in.
- `cache-warning`: exit 0 with a warning only, which the doctor does not read as
  signed in.
- `warning-word`: exit 0 with one word on stderr, which the doctor does not read
  as signed in.
- `models-with-warning`: exit 0 with the model list on stdout and a warning on
  stderr, which the doctor reads as signed in.

`agy models` sends no prompt to a model.
