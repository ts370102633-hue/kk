# StepAudio Voice Studio Deployment Notes

## Persistent Data Rule

Do not store production data inside the code checkout directory.

Recommended cloud server layout:

- Code: `/opt/kk-studio`
- Config: `/etc/stepaudio/stepaudio.env`
- Database: `/var/lib/stepaudio/stepaudio.db`
- Generated/downloaded files: `/var/lib/stepaudio/files`

This keeps user accounts, credits, invite codes, admin password, task history, voice records, and downloaded files safe when code is replaced during deployment.

## Required Environment

Create `/etc/stepaudio/stepaudio.env`:

```bash
APP_ENV=production
DATABASE_URL=sqlite:////var/lib/stepaudio/stepaudio.db
LOCAL_STORAGE_DIR=/var/lib/stepaudio/files
STEP_API_KEY=your_stepaudio_api_key
STEP_API_BASE=https://api.stepfun.com/step_plan/v1
STEP_FILE_API_BASE=https://api.stepfun.com/v1
STEP_ASR_MODEL=stepaudio-2.5-asr
STEP_TTS_MODEL=stepaudio-2.5-tts
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace-with-a-strong-password
BILIBILI_COOKIE=
DOUYIN_COOKIE=
XHS_COOKIE=
VIDEO_BROWSER_CHANNEL=
VIDEO_HD_MIN_SHORT_SIDE=1080
VIDEO_YTDLP_FALLBACK_ENABLED=true
VIDEO_DOUYIN_SHORTCUT_FALLBACK_ENABLED=false
VIDEO_SHORTCUT_API_ENABLED=true
VIDEO_SHORTCUT_ORIGINAL_FIRST_ENABLED=false
VIDEO_REQUIRE_ORIGINAL_API=false
VIDEO_SHORTCUT_DAILY_LIMIT=20
VIDEO_SHORTCUT_RATE_LIMIT_COOLDOWN_SECONDS=300
VIDEO_SHORTCUT_CONFIG_URL=https://qsy.jiejing.fun/qsy.json
VIDEO_SHORTCUT_API_BASE=https://a.jiejing.fun
VIDEO_SHORTCUT_AUTH_CODE=
TIKHUB_ENABLED=false
TIKHUB_ORIGINAL_FIRST_ENABLED=false
TIKHUB_API_BASE=https://api.tikhub.dev
TIKHUB_API_KEY=
TIKHUB_DOUYIN_REGION=CN
TIKHUB_TIMEOUT_SECONDS=45
```

Protect the file:

```bash
chmod 600 /etc/stepaudio/stepaudio.env
```

## Backup

Back up both the database and generated files:

```bash
mkdir -p /root/stepaudio-backups
sqlite3 /var/lib/stepaudio/stepaudio.db ".backup '/root/stepaudio-backups/stepaudio-$(date +%F-%H%M).db'"
tar -czf "/root/stepaudio-backups/stepaudio-files-$(date +%F-%H%M).tar.gz" /var/lib/stepaudio/files
```

## Restore

Stop the service before restore:

```bash
systemctl stop stepaudio
cp /root/stepaudio-backups/stepaudio-YYYY-MM-DD-HHMM.db /var/lib/stepaudio/stepaudio.db
tar -xzf /root/stepaudio-backups/stepaudio-files-YYYY-MM-DD-HHMM.tar.gz -C /
systemctl start stepaudio
```

## Operational Checks

After deployment:

```bash
systemctl status stepaudio
curl http://127.0.0.1:8000/health
ls -lh /var/lib/stepaudio/stepaudio.db
```

The database file must stay in `/var/lib/stepaudio`, not inside `/opt/kk-studio`.

## Optional Video Quality Cookies

Some platforms only return higher-quality streams to logged-in clients.

Optional cookie fields:

- `BILIBILI_COOKIE`: useful for Bilibili 1080P high bitrate, 4K, membership-only quality levels, when the account has permission.
- `DOUYIN_COOKIE`: may improve access to mobile/web streams for videos that require login.
- `XHS_COOKIE`: may improve access to Xiaohongshu videos that require login.
- `VIDEO_BROWSER_CHANNEL`: optional. Use `chrome` on machines with Google Chrome installed; leave blank on Linux servers using Playwright's bundled Chromium.
- `VIDEO_HD_MIN_SHORT_SIDE`: short-side threshold for treating a Douyin/Xiaohongshu result as high quality. Default `1080` means `720x1280` is considered below threshold, while `1080x1920` and `2160x3840` are high quality.
- `VIDEO_YTDLP_FALLBACK_ENABLED`: enables `yt-dlp` as a Douyin low-quality/failure fallback after the built-in parser. Default `true`.
- `VIDEO_DOUYIN_SHORTCUT_FALLBACK_ENABLED`: enables the third-party shortcut-style Douyin parser after built-in and `yt-dlp` fail. Default `false` to avoid consuming limited shortcut quota.

Admins can also configure `DOUYIN_COOKIE` and `XHS_COOKIE` from the web admin panel under `平台 Cookie`. Values saved there are stored in the persistent SQLite database and take priority over blank environment variables. API responses only return masked previews, not the full cookie.

Leave these blank unless you understand the account and platform risk. Do not use third-party cookies or download videos without legal authorization.

## Optional Shortcut-Style Original Quality Parser

`VIDEO_SHORTCUT_API_ENABLED=true` allows Douyin and Xiaohongshu to use the same style of cloud parser used by the inspected iOS Shortcut as a fallback when the built-in parser fails or only finds a below-threshold result.

Operational notes:

- `VIDEO_SHORTCUT_CONFIG_URL` loads the parser endpoint map.
- `VIDEO_SHORTCUT_API_BASE` is used when the config endpoint cannot be reached.
- `VIDEO_SHORTCUT_AUTH_CODE` can hold a purchased/authorized parser code if the provider requires it.
- `VIDEO_SHORTCUT_DAILY_LIMIT` tracks the parser's daily high-quality quota locally and stops calling it after the configured limit is reached.
- `VIDEO_SHORTCUT_RATE_LIMIT_COOLDOWN_SECONDS` treats provider HTTP 429 as temporary rate limiting and pauses calls for this many seconds instead of locking out the whole day.
- `VIDEO_SHORTCUT_ORIGINAL_FIRST_ENABLED=true` makes Douyin/Xiaohongshu call the shortcut-style original-quality parser before built-in parsing.
- `VIDEO_REQUIRE_ORIGINAL_API=true` prevents silent low-quality fallback when the original-quality parser is unavailable; the video task fails and refunds the credit instead.
- Set `VIDEO_SHORTCUT_API_ENABLED=false` to disable this dependency and use only the built-in browser/API extraction.

TikHub is staged but disabled by default:

- `TIKHUB_ENABLED=true` enables TikHub API calls.
- `TIKHUB_ORIGINAL_FIRST_ENABLED=true` makes TikHub run before the shortcut-style original-quality parser.
- `TIKHUB_API_BASE=https://api.tikhub.dev` is recommended for the Aliyun China mainland deployment. Use `https://api.tikhub.io` only for non-mainland servers.
- `TIKHUB_API_KEY` is sent as `Authorization: Bearer ...`; keep it only in server environment files.
- Douyin uses `/api/v1/douyin/web/fetch_video_high_quality_play_url`.
- Xiaohongshu uses `/api/v1/xiaohongshu/app_v2/get_video_note_detail`.

Business risk: this parser is a third-party service. Treat it as an external dependency, confirm usage rights before production use, and assume it may have daily limits, pricing, downtime, or policy changes.
