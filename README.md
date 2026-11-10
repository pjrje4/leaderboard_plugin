leaderboard (1.0, Plugin Releases/leaderboard.py) shows ranks from the Eschaton Eye all-time callout leaderboard in StarBreak. It’s one file you drop into a vanilla SBPE’s plugins/ folder.

What you see in game

Name tags: players on the leaderboard get their rank after their name, like [6] Jekyll [2403]. Players who aren’t on it, and new sparks, are left alone.
Faction roster: the rank appears after each member’s name, in the same font and color as the name.
STATUS box (Tab): two new rows at the top show your own Leaderboard Rank and Leaderboard Points. The box grows to fit.

How it stays current

It downloads the leaderboard page at most once a day, in the background, so the game doesn’t stall. It does this when the game starts if today’s copy isn’t saved yet, and again if the date changes while you play. A failed download is retried every 30 minutes.
It reads chat for callouts like “The Elite Graveyard has been defeated! X gave an impressive performance!” and adds that zone’s reward from the page to X’s points. Ranks update right away, both for that player and for you if they pass you.
It saves two files in the SBPE folder:
leaderboard_cache.json holds the downloaded page.
leaderboard_mirror.json holds the callouts seen since that download, so a restart the same day keeps them.

Settings ([plugin_leaderboard] in config.ini)

name_tags: rank after name tags
roster: rank in the faction roster
tab_lines: the two rows in the STATUS box
decimals: decimals shown for your points

Safety

It doesn’t permanently change game code; it only calls the game’s own functions.
Before using each game function it checks that the function is where it expects. If a game update moves one, it turns that feature off and logs why instead of crashing.
It runs fine next to faction.py and works without it.

Known limits

Zones with no reward on the page (non-elite Frozen Blight and Fire Forest) add no points.
If you live-reload the plugin while the Tab screen is open, the STATUS rows can shift down twice until you reopen it.
