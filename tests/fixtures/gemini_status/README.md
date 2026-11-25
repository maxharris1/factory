Replies of `agy models </dev/null 2>&1`, the doctor's Gemini status probe,
run with every Gemini auth override and `GEMINI_CLI_HOME` unset. Each exit
code is in `<name>.code`. The replies come from the #1639 live check of
Antigravity `agy` 1.5.15:

- `signed-in`: the device's `gemini-bot` login is signed in, so `agy models`
  exits 0 and lists the models on stdout. The check recorded only that shape,
  so these model names were written from it and not captured. A later live
  capture can replace them.
- `signed-out`: no login, exit 1 with the CLI's exact sign-in error.
- `no-models`: exit 0 with no output, which the doctor does not read as signed in.
- `cache-warning`: exit 0 with a warning only, which the doctor does not read as
  signed in.

`agy models` sends no prompt to a model.
