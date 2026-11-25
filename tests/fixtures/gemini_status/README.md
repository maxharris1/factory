Replies of `env NO_BROWSER=true NO_COLOR=1 gemini </dev/null 2>&1`, the
doctor's Gemini status probe, with each exit code in `<name>.code`. The text
and codes come from upstream gemini-cli at
08e9a20f749aefcbda38622ac757899c3d6216e0:

- `signed-in`: cached Google sign-in loads (`packages/cli/src/gemini.tsx`,
  `getOauthClient` before the input check), then the empty stdin exits with
  `FATAL_INPUT_ERROR` (42). No prompt reaches a model.
- `no-auth-method`: no auth type in the folder's `settings.json`
  (`packages/cli/src/validateNonInterActiveAuth.ts`), exit
  `FATAL_AUTHENTICATION_ERROR` (41).
- `needs-login`: Google sign-in is selected but no cached login is in the
  folder (`packages/core/src/code_assist/oauth2.ts`, non-interactive with
  `NO_BROWSER`), exit 41.
