# Reclaimer

Finds what got lost in a Plex media library (the 4K episode in a 720p season, the movie ten times the size it deserves, the audio tracks nobody listens to) and cleans it up. Scans with ffprobe, recommends where space can be saved, and re-encodes on the GPU to HEVC that every device in the house direct-plays (Xbox One X, Roku Ultra, iPhone/iPad, Android, PC).

## Setup (Windows main PC)

```
winget install Python.Python.3.12
winget install Gyan.FFmpeg
```

Or just double-click **start.bat**: it installs both with winget if they're missing, then starts the app. If winget isn't available it opens the ffmpeg download page instead, and the app itself shows a setup page with a **Check again** button until ffmpeg is found (copying `ffmpeg.exe` and `ffprobe.exe` next to Reclaimer is enough).

```
python reclaimer.py
```

That opens `http://localhost:8765` in your browser. Reclaimer then lives in the **system tray** next to the clock, with no console window (start.bat installs the small tray helper, `pystray`, the first time):

- click the icon to open the page; hover it to see what it's doing (blue = idle, green = encoding)
- right-click for Start queue / Stop after current file, **Start with Windows**, and Quit
- you get a Windows notification when a batch finishes (switch it off in the tray menu or Settings → Schedule)

Messages that used to go to the console are written to `reclaimer_run.log`. To run it the old way in a terminal, use `python reclaimer.py --no-tray`. `build_exe.bat` turns it into a single `reclaimer.exe` if you'd rather not have Python around (ffmpeg.exe/ffprobe.exe can sit next to it).

## First run

1. The first page asks for your first library folder, e.g. `\\mynas\video\Movies` (movies, Movies HD). Add the rest in **Settings** — one row per folder, e.g. `\\mynas\video\TV` (tv, Live Action TV) and the Radarr/Sonarr folders.
2. **Recommendations** tab: pick a scope (all, or one library) and hit **Scan**. Big libraries take a while the first time; later scans only probe files that changed.
3. Each row is a show or movie. Click it to see the files. Change the preset dropdown on anime/cartoon shows (select several rows and use "Set preset for selected" to do it in bulk).
4. Not sure about a preset? Click **preview** next to any file: a 20-second clip from the middle is encoded with the current settings and opened in your video player (also linked from the page).
5. Tick what you want, **Queue selected**, then **Start** on the Queue tab. Try a handful of files first and check them in Plex.
6. Happy with a show? Leave it. Not happy? **restore** on its row puts the originals back. Once you trust the results, **Delete all** in the originals card frees the space.

## Running on its own

Settings → Schedule: limit encoding to a window (say 1:00–8:00) and pause while someone is watching Plex (it checks the Plex sessions every minute, since encoding reads big files off the same NAS). Turn on **auto mode** and it rescans every 30 minutes, queues whatever your presets recommend and encodes it inside the window — leave the window open and new Sonarr/Radarr downloads get handled. **Start now** on the Queue tab ignores the schedule for one run.

## Automatic presets from genres

Every scan reads the `.nfo` files Sonarr, Radarr and Kodi keep next to your media: `tvshow.nfo` in a show's folder, `movie.nfo` or `<movie name>.nfo` in a movie's folder. If a show has no `tvshow.nfo`, the genres at least half of its episode NFOs agree on are used. A show always gets one preset as a whole, even if single episodes are tagged differently.

These genres are combined with Plex's (below), then matched against the rules table in Settings → Plex. Hover over the tag next to a preset to see which genres it came from. A yellow tag means a show or movie has genres but no rule matched them; add a rule for those genres if you like. After each scan, a message says how many shows and movies got their preset from genres.

## Plex genre sync (optional)

Instead of hand-picking Anime/Cartoons shows, let Plex do it. In Settings → Plex enter your server URL (`http://mynas:32400`) and token, scan the library first, then **Sync genres**. Every show and movie gets a preset by the rules table. Defaults: *Anime* → Anime; *Documentary + Crime* (or *True Crime*) → True Crime; *Animation* → Cartoons HD for TV, CG Animation for movies (Cartoons SD is picked automatically for SD-only content); *Comedy* TV → Sitcom. `A+B` in a rule means the item needs both genres. Cinematic TV has no rule, since Plex genres can't tell an HBO drama from a procedural; assign it by hand. The sync also runs on its own after every full scan, and its status line says how many items matched and gives an example of one that didn't. First matching rule wins; manual overrides in the Recommendations tab always win over Plex. Re-run the sync whenever Plex gains new shows.

Token: in Plex Web open any item → ⋯ → Get Info → View XML, and copy the `X-Plex-Token` value from that page's URL.

With "rescan Plex libraries when the queue finishes" ticked, Plex is asked to rescan after each batch so it picks up the new files right away.

## How it decides

- A file is recommended when the preset's limits (max resolution, bitrate cap, audio policy) would save at least 15% (Settings → min savings).
- **Show rule:** in each season, the most common resolution is the "typical episode". Episodes above it are brought down to that resolution and to the season's median bitrate, even if the preset would allow more. Nothing is ever upscaled.
- HDR becomes SDR on every preset except Movies 4K (tone-mapped on the GPU via libplacebo, or on the CPU if that isn't available).
- Audio: **stereo** presets produce AAC 2.0; **surround** presets keep AAC/AC3/E-AC3 up to 5.1 and turn TrueHD/DTS/FLAC/PCM into E-AC3 5.1. If a file has a lossless track plus a compatible one in the same language, the lossless one is simply dropped.
- **Burn subs** (Anime preset: ASS/SSA only): styled ASS/SSA subtitles can't be shown by the Roku or Xbox Plex apps without a server transcode, which the NAS can't do well. If a file has a full (non-forced, non-"signs & songs") ASS track in your language (Settings → burn subs language, default `eng`) but no audio in that language, the track is rendered into the picture with the file's embedded fonts and removed as a separate track. Plain SRT subs play everywhere and are never burned; dubbed files and files that already carry an SRT are left alone. The "ASS + PGS/VobSub" option also burns image subs. Burn jobs run even when they wouldn't save space.
- Subtitles, chapters and fonts are copied. Output is always `.mkv` with the same filename, so Sonarr/Radarr still see the same release and won't re-download.

## Audio languages

Settings → Audio languages lists every audio language in your scanned libraries, with how many files have it and how much space it takes. Turn on **remove audio in languages I don't keep** and tick the ones to keep. A track is always kept if:

- it's the file's only audio track, or none of the file's tracks are in a language you keep (nothing is ever left silent);
- it's flagged as the default track, usually the original language (can be switched off);
- it has no language tag (can be switched off).

When the video is already within the preset's limits, the file is only repacked without the dropped tracks. That takes seconds and the video is untouched.

## What ffmpeg is for

ffmpeg (with ffprobe) does all the actual video work: reading each file's details during a scan, and decoding, scaling, tone-mapping, burning subs, NVENC encoding, audio conversion and writing the files. Reclaimer decides what to do and runs it. Without ffmpeg it can't scan or encode at all. ffmpeg is free; if you ever pass ffmpeg.exe on to someone else, include its license (GPL) and a link to its source.

## Sonarr and Radarr

If Reclaimer shrinks an episode below the quality Sonarr is set to want (the profile's *cutoff*), Sonarr can decide it needs upgrading and download the big copy again. Add their URLs and API keys in Settings → Sonarr / Radarr (the key is under Settings → General → Security in each app). Reclaimer then:

- never downscales a file they manage below its profile's cutoff (it still lowers the bitrate). The Health tab lists which profiles held files back, so you can lower a cutoff if you'd rather go smaller;
- prefers keeping the copy they manage when removing duplicates;
- asks them to rescan the shows and movies it changed.

## Cleanup tab

**Duplicates**: the same movie or episode in more than one place, typically once in your old library and once in the Sonarr/Radarr one, or two versions of a movie in one folder. Matching uses the ids in NFO files and folder names (`{tmdb-603}`), then title and year. Multi-episode files and cd1/cd2 parts are never counted as duplicates. Reclaimer suggests a copy to keep; pick another with the radio buttons. It will never remove every copy of something.

**Junk**: sample clips, unfinished downloads and stale temp files, archives that were already extracted, release clutter (`.txt`, `.url`, `.exe`…), and, unticked by default, stray subtitle files, extras and show/movie folders with no video left.

Both move things to `.reclaimer_removed` inside the library, which **restore** on the Recommendations tab and **Delete all** in the held card cover too.

## Health tab

- Space reclaimed so far, how much is still sitting in holding folders, and an estimate of what's left to reclaimer, with a chart over time and a per-library breakdown.
- **Plex transcodes**: while Reclaimer runs it checks Plex every 30 seconds, and lists anything Plex had to convert on the fly instead of direct-playing: which device, and whether it was the video, audio or subtitles. Files that keep appearing here are the ones to fix first; **queue fix** queues the file if Reclaimer has a recommendation for it.
- The Sonarr/Radarr status.

## Protecting files (family videos etc.)

Two ways to make Reclaimer never touch something:

- Click **protect** under a show or movie's name on the Recommendations tab. Protected items stay listed (untick "only show items with savings" to see them) but are skipped by scans, the queue and auto mode, even if they were already queued.
- Or put an empty file named `.reclaimerignore` in a folder. That folder and everything below it is left out of scans entirely. Good for a whole "Home Videos" folder.

The simplest option of all: anything outside the library folders you add in Settings is never looked at.

## Checking the results

Every encode is checked before it replaces anything (Settings → Check each encode):

- **full** (default): the whole new file is decoded to catch corrupt or cut-off video and broken audio; every audio track must run the full length; and frames from 10%, 30%, 50%, 70% and 90% through are compared with the original (scaled, deinterlaced and tone-mapped the same way) using SSIM. If any of them scores below 0.80 (green or black frames, garbage, or picture out of sync with the source), the encode is thrown away and the original stays.
- **quick**: only the audio-length and picture checks.
- **off**: only the basic duration and track-count checks.

A good encode typically scores 0.93-0.99, and the score is shown in the queue and the log. Before you empty the holding folders, **Health → Verify held files** re-checks every encoded file against its held original, and **Delete all** reminds you if you haven't run it or if anything failed.

## Safety

- Encodes go to a hidden temp file and only replace the original after a duration and audio-track check, and only if actually 15%+ smaller. Ctrl+C or **Cancel current** leaves the original untouched.
- Originals are moved to `.reclaimer_originals` inside each library, with `.plexignore`/`.ignore` files so Plex ignores them. **No space is freed until you click Delete all** (or delete that folder). Each show/movie row has a **restore** link while its originals are held. Switch Settings → Originals to "delete" once you trust the results.
- Every output carries a `RECLAIMER` tag so it's never encoded twice. Failed or rejected files are remembered (`reclaimer_state.json`); click "retry" next to them to try again.
- `reclaimer_log.csv` has the history of every encode.

## Presets (defaults, editable in the GUI)

| Preset | Max | 1080 cap | 720 cap | 480 cap | CQ | Audio | Burn subs |
|---|---|---|---|---|---|---|---|
| Live Action TV | 1080p | 5 | 3 | 1.5 | 26 | stereo | none |
| Anime | 1080p | 3.5 | 2 | 1.2 | 25 | stereo | ASS |
| Cartoons HD | 1080p | 3 | 1.8 | 1 | 27 | stereo | none |
| Cartoons SD | 480p | | | 1 | 27 | stereo | none |
| Movies HD | 1080p | 8 | 4 | 2 | 24 | surround | none |
| Movies SD | 480p | | | 1.5 | 25 | surround | none |
| Movies 4K | 2160p (HDR kept) | 8 | 4 | 2 | 24 | surround | none |
| Cinematic TV | 1080p | 7 | 4 | 2 | 24 | stereo | none |
| Sitcom | 1080p | 3 | 2 | 1.2 | 27 | stereo | none |
| True Crime | 720p | | 1.8 | 1 | 28 | stereo | none |
| CG Animation | 1080p | 5 | 3 | 1.5 | 25 | surround | none |

Caps are in Mbps. With CQ encoding most files land well under the cap; the cap only stops action scenes from ballooning.
