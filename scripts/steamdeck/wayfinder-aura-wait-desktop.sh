#!/bin/sh
# Aura reads the desktop's portals once at startup and never re-checks them.
# Leaving Game Mode it can start before Plasma publishes its environment, and
# the portal it wakes then serves gamescope's backends for the whole session:
# no global shortcuts, no typing into native apps. Game Mode needs no wait.
#
# Worst case is 109s: 63 one-second sleeps plus 23 busctl calls of up to 2s.
# wayfinder-aura.service caps it with timeout(1), and its TimeoutStartSec must
# stay above that cap. This is best effort, so it always exits 0.
has_shortcuts() {
  busctl --user --timeout=2 call org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop \
    org.freedesktop.DBus.Properties Get ss org.freedesktop.portal.GlobalShortcuts version >/dev/null 2>&1
}
i=0
while [ "$i" -lt 40 ]; do
  systemctl --user -q is-active gamescope-session.service && exit 0
  systemctl --user show-environment | grep -q '^XDG_CURRENT_DESKTOP=KDE' && break
  sleep 1; i=$((i + 1))
done
i=0
while [ "$i" -lt 8 ]; do
  has_shortcuts && exit 0
  sleep 1; i=$((i + 1))
done
echo 'portal has no GlobalShortcuts, restarting it'
systemctl --user --no-block restart xdg-desktop-portal.service
i=0
while [ "$i" -lt 15 ]; do
  has_shortcuts && exit 0
  sleep 1; i=$((i + 1))
done
exit 0
