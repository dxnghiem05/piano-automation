# TikTok Direct Post Audit — Application Pack (PianoClip)

Everything you need to apply for the **Direct Post audit** so captions/hashtags carry over automatically and clips can post straight to your profile. Work through the three parts below.

---

## 1. Where to apply

TikTok Developer Portal → your **PianoClip** app → **Content Posting API** section → next to **Direct Post**, click **Apply**. You'll be asked for (a) a written explanation of how you use the API and scopes, and (b) a demo video. Use Parts 2 and 3 for those.

Keep the app's Direct Post option enabled and **leave your existing scopes** (`user.info.basic`, `video.upload`, `video.publish`, `video.list`) as they are.

---

## 2. Written description (paste this into the application)

> **PianoClip** is a personal creator tool used only by its owner (Dustin Nghiem) to manage and repost his own piano performance clips to his own TikTok account (@dustinspiano). It is not a multi-tenant product; there is a single authorized creator.
>
> **Workflow:** The tool tracks how the owner's short piano clips perform on YouTube, surfaces the best-performing clips on a private dashboard, and lets the owner send those same clips to his own TikTok account. The owner reviews each clip and its caption before anything is published.
>
> **How each product/scope is used:**
> - **Login Kit — `user.info.basic`:** After the owner authorizes, we call `/v2/user/info/` to display the connected account's `display_name` and `avatar_url` on the dashboard, so the owner can confirm which account they are posting to before publishing (per the UX guidelines).
> - **Content Posting API — `video.publish` (Direct Post):** Used to post the owner's own piano clips directly to his own TikTok profile. Before every post we call `/v2/post/publish/creator_info/query/` to fetch the account's allowed privacy levels and creator settings, present them to the owner, and only publish after the owner confirms. The caption is the clip's title plus a small set of relevant hashtags (e.g. #piano #pianocover). We use `source=FILE_UPLOAD` (push_by_file) to upload the local clip.
> - **Content Posting API — `video.upload`:** Used as the pre-audit path — uploading a clip to the owner's TikTok inbox as a draft for the owner to finish and post in the app.
> - **Display API — `video.list`:** Used to read the owner's own public TikTok videos and their view/like/comment/share counts, shown on a private "TikTok Stats" page so the owner can compare TikTok performance against YouTube.
>
> **Compliance:** Only the owner's own content is posted, to the owner's own account, after explicit review. We display the creator's nickname before posting, honor the privacy levels returned by `creator_info`, disable/enable interaction settings only as the owner chooses, and show the Music Usage Confirmation notice. No third-party or automated bulk posting occurs; every post is an explicit, owner-initiated action.

*(Edit the names/handles if anything is off. Keep it factual — reviewers reject vague or overbroad descriptions.)*

---

## 3. Demo video (record this, ~1–2 min, screen recording)

TikTok requires a demo showing the **complete end-to-end Direct Post flow on the actual website/domain you registered**. Record your dashboard at your real callback domain and show, in order:

1. **Open the dashboard** and click **Owner login** (show it's your real app/site).
2. Click **Connect TikTok** → the TikTok authorization screen → approve. Show the consent screen listing the scopes.
3. Back on the dashboard, show the **connected account's name/avatar** displayed (proves `user.info.basic`).
4. On **TikTok Candidates**, pick a clip and start a post. Show the UI that displays:
   - the creator nickname you're posting as,
   - the **caption** (title + hashtags),
   - the **privacy level** selector (values from `creator_info`),
   - the **Music Usage Confirmation** text.
5. Confirm and post. Then show the video appearing on the **TikTok profile**.
6. (Optional but strong) Show the **TikTok Stats** page pulling the post's view/like counts (`video.list`).

**Recording rules that cause rejections if missed:**
- The website domain in the video must match your registered Web URL / redirect URI.
- Show real UI and real interactions (no slides/mockups).
- Every scope you keep enabled must be demonstrated — if you don't demo it, remove it first or the review is delayed.
- Keep each file under 50 MB, mp4/mov, up to 5 files.

---

## 4. After approval

Tell me once the audit is approved. I'll flip the dashboard's "Send to TikTok" button from draft-upload back to **Direct Post** (the code is already there), wire in the auto caption + hashtags, and add the privacy-level selector so it satisfies the compliance UI. Nothing needs re-connecting — your token already has `video.publish`.
