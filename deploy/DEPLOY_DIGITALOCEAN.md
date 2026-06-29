# Put the bot online 24/7 with DigitalOcean (~$5/month)

You do the account + payment (I can't), I guide every screen. With the
self-contained installer, there's **no file upload and no SSH** — you paste
one script when creating the server and it does everything.

---

## Step 1 — Make a DigitalOcean account
- Go to **digitalocean.com**, sign up (email + a card). New accounts often get
  free credit.

## Step 2 — Create the server (a "Droplet")
- Click **Create → Droplets**.
- Choose **Ubuntu** (newest LTS), Region near you.
- Size: the cheapest **Basic / Regular** (~$4–6/mo) is plenty.
- Authentication: **Password** is simplest (set one you'll remember).
- **IMPORTANT — paste the installer:** scroll to **Advanced options → Add
  Initialization scripts (user data)**. Open the file **`cloud_oneshot.sh`**
  (from your Kalshi Bot folder) in Notepad, select all (Ctrl+A), copy
  (Ctrl+C), and paste it into that box.
- Click **Create Droplet**. Wait ~3 minutes while it builds and runs the
  installer automatically.

## Step 3 — Get your dashboard link
- On the Droplet page, note its **IP address**.
- Click **Access → Launch Droplet Console** (a terminal opens in your browser).
- Log in (user `root`, the password you set).
- Type:  `cat /root/DASHBOARD_LINK.txt`  and press Enter.
- It prints your link, like:  `http://YOUR_IP:8765/?token=abc123`

## Step 4 — Open + share it
- Open that link in any browser, on any device. That's your live dashboard.
- Share it by sending the full link (the token is its password — keep it
  private if you want it semi-private).

---

## It just works from here
- The bot runs 24/7 and restarts itself if the server reboots.
- To stop paying: delete the Droplet in DigitalOcean — billing stops at once.

## Handy commands (in the console)
```
systemctl status kalshi-paper      # is the bot running?
journalctl -u kalshi-paper -f      # watch it live (Ctrl+C to stop watching)
systemctl restart kalshi-paper     # restart it
```
