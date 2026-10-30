# Connecting the assistant to Google (Gmail and Calendar)

The dashboard already contains the whole Google sign-in flow (`/api/v1/assistant-apps/...`). `herdr-eng google` sets it up
and re-sets it up from a terminal, through those same routes. Nothing about the secret is ever an argument or printed.

```
herdr-eng google status                      # what is done, what is not, and the Google Console steps still to do
herdr-eng google set-client --json-file ~/Downloads/client_secret_XXXX.json
herdr-eng google connect                     # prints the sign-in link; open it in a browser on the tailnet
herdr-eng google disconnect --yes            # signs the assistant out (the stored client stays)
```

`set-client` also accepts `--client-id ID --client-secret-file PATH`, or `--client-id ID` and a hidden prompt for the secret.
It warns when the downloaded client does not list the redirect URI the dashboard uses.

## Google Cloud Console (cannot be automated)

1. Enable the Gmail API and the Google Calendar API.
2. Credentials: a **Web application** client whose Authorized redirect URI is exactly the one `status` prints
   (`<personal_assistant.apps.redirect_base>/api/v1/assistant-apps/google/callback`).
3. OAuth consent screen > Audience: while in **Testing**, add each account that will connect under *Test users*, or Google
   refuses the sign-in. Testing mode expires refresh tokens after 7 days: switch to **In production** to keep them.
4. Google shows "Google hasn't verified this app" for the Gmail scopes until the app is reviewed. For personal use click
   *Advanced > Go to (app)*; review is not needed under 100 users.

## Re-configuring

Rotating the secret: download the new JSON, run `set-client` again, then `connect` if `status` shows the account needs
attention. Changing the host name: update `personal_assistant.apps.redirect_base`, redeploy, add the new redirect URI in
the Console, and run `status`.
