# How to test a real Crisp chat widget

This test checks that the harness can talk to a real chat widget. You reply by hand from the
Crisp inbox, so no AI or paid plan is needed. It takes about 10 minutes.

You need: this repo installed (see the main README), Chromium (`playwright install chromium`),
and a second browser tab or your phone for the Crisp inbox.

Run every command from this folder (`examples/real_widget_test/`).

1. **Make a free Crisp account** at crisp.chat. Then copy the chatbox script:
   Settings → Workspace Settings → Setup & Integrations → HTML. It is a short `<script>` block.
2. **Paste it into `index.html`**, in place of the line
   `<!-- Paste your Crisp chatbox script here -->`. Do not share or commit this file afterwards:
   the script holds your Crisp website ID.
3. **Start a small web server** in this folder:
   ```
   python -m http.server 8000
   ```
   Leave it running. Open http://localhost:8000 in your browser and check that the Crisp chat
   bubble shows in the corner.
4. **Open the Crisp inbox** in another tab or in the Crisp app on your phone.
5. **Run the check** in a second terminal, from this folder:
   ```
   audit check --config client_config.yaml
   ```
   A browser window opens. When the terminal says it sent "Hello" (or prints "Still waiting for a
   reply"), reply from the Crisp inbox with:
   `Hi, I'm the Lumen AI assistant.`
   You have 90 seconds.
6. **If it fails**, run the check once more with debug output:
   ```
   audit check --config client_config.yaml --debug
   ```
   Then record the selectors by clicking on the page:
   ```
   audit connect --url http://localhost:8000/
   ```
   Follow the prompts. When it says it sent "Hello", reply from the Crisp inbox again.
7. **Send your reviewer** these files from this folder:
   - the terminal output (copy and paste it)
   - `check_screenshot.png`
   - the `check_debug/` folder (if it was made)
   - `target_recorded.yaml` (if it was made)

When you are done, stop the web server with Ctrl+C and remove the Crisp script from `index.html`.
