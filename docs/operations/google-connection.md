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
4. The requested scopes are `openid`, `userinfo.email`, `calendar.readonly`, `calendar.events`, `gmail.readonly` and
   `gmail.compose` (compose also allows sending). The Gmail scopes are RESTRICTED: a verified app would need Google's paid
   annual security assessment. This deployment stays unverified instead (up to 100 users, personal use): leave the app in
   *In production* but do not submit it for verification. Google shows "Google hasn't verified this app" once at sign-in:
   *Advanced > Go to (app)*.
5. Sending: the assistant can send mail only through the `send_email` tool, which always raises an approval card showing the
   exact recipients, subject and body; nothing is delivered before a person approves. Replies keep their thread.
6. A connection that lacks the Gmail scopes (made earlier, or refused on the consent screen) still serves the calendar; the
   Gmail tools answer `forbidden` before any request, and the dashboard's Gmail card shows "not connected".

## Re-configuring

Rotating the secret: download the new JSON, run `set-client` again, then `connect` if `status` shows the account needs
attention. Changing the host name: update `personal_assistant.apps.redirect_base`, redeploy, add the new redirect URI in
the Console, and run `status`.
