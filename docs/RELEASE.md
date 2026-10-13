# TENDRIL v0.2.1 release runbook

Everything on the `release/v0.2.1` branches is ready except the ONE human
secret: the production signing key. This runbook is the exact procedure to
finish the release. Nothing here may be automated past the key boundary —
the key is created by a human, on a human's machine, and never enters git,
CI logs, or this repository.

## 0. What is already in place (no key needed)

- `phone/setup.sh` — the public Termux command. Pins the release signer via
  the `@LINK_SIGNER_SHA256@` placeholder and refuses to run while it is
  unstamped (`This setup script has no release signer pinned`).
- `bin/tendril_phone.py` — `LINK_RELEASE_SIGNER = ""`. `tendril --link-fetch`
  refuses while it is empty (`no release signer is pinned in this TENDRIL
  version`), and `--link-stage` verifies every APK's ACTUAL signing block
  against its manifest (a manifest can lie; a signing block cannot).
- Link repo `scripts/build-release.sh` — signed release build + dist
  artifacts + schema-1 manifest; refuses without `keystore.properties`.
- Link repo `app/build.gradle.kts` — release signing activates only when
  `keystore.properties` exists (gitignored). Without it, `assembleRelease`
  builds unsigned (smoke builds only).

## 1. Create the production key (HUMAN ACTION — stop here until done)

On the release operator's machine, NOT in any repo checkout, NOT on CI:

```sh
# 1a. make a directory outside every repository
mkdir -p ~/secrets/tendril-release && chmod 700 ~/secrets/tendril-release

# 1b. generate the key (RSA 4096, ≥25 years so updates never expire).
#     CHOOSE STRONG PASSWORDS — there is no recovery.
keytool -genkeypair -v \
  -keystore ~/secrets/tendril-release/tendril-link-release.jks \
  -alias tendril-link -keyalg RSA -keysize 4096 -validity 10950 \
  -dname "CN=TENDRIL Link, O=TENDRIL, C=NL"
```

- Keep `~/secrets/tendril-release/tendril-link-release.jks` and both
  passwords in a password manager AND one offline backup.
- **Never commit or copy the `.jks`, the passwords, or `keystore.properties`
  into any repository.** The link repo's `.gitignore` already excludes
  `*.jks`, `*.keystore`, and `keystore.properties`; keep it that way. Losing
  the key means every existing installation must uninstall before updating.

## 2. Describe the key to the build (local, gitignored)

In the **link repo** root (`~/Projects/tendril-link`), create
`keystore.properties` (already gitignored):

```properties
storeFile=/home/USERNAME/secrets/tendril-release/tendril-link-release.jks
storePassword=...
keyAlias=tendril-link
keyPassword=...
signerSha256=TO-BE-FILLED-IN-STEP-3
```

## 3. Derive the public signer SHA-256 (the pin)

The pin is the SHA-256 of the certificate DER — public by nature. The
simplest honest derivation is from a first signed build:

```sh
cd ~/Projects/tendril-link
./gradlew :app:assembleRelease --no-daemon      # FIRST signed build (pin not needed yet)
"$(ls -d "$HOME/Android/Sdk/build-tools/"* | sort -V | tail -1)/apksigner" \
  verify --print-certs app/build/outputs/apk/release/app-release.apk | grep "SHA-256"
```

`keytool -list -v -keystore ~/secrets/tendril-release/tendril-link-release.jks
-alias tendril-link | grep "SHA256:"` gives the same value. Take the
certificate (not the public-key) digest, lowercase, no colons — 64 hex
characters. Put it in `keystore.properties` (`signerSha256=`), re-run
`scripts/build-release.sh` fully (with tests, with staging) and confirm the
final block prints `signer: <64 hex> (pinned release key)`.

## 4. Stamp the pin into the host repo (one value, two places)

In the **host repo** (`~/Projects/tendril`), with `<PIN>` = the 64-hex value:

```sh
sed -i 's/^LINK_SIGNER=@LINK_SIGNER_SHA256@$/LINK_SIGNER=<PIN>/' phone/setup.sh
sed -i 's/^LINK_RELEASE_SIGNER = ""$/LINK_RELEASE_SIGNER = "<PIN>"/' bin/tendril_phone.py
python3 -m unittest tests.test_phone_channel tests.test_pair tests.test_enroll
```

All three references (keystore.properties, phone/setup.sh,
tendril_phone.py) must carry the identical value; the tests refuse a
mismatch. Commit: `chore(release): pin the TENDRIL Link release signer`.

## 5. Cut the release (only when the release is actually happening)

```sh
# host repo
./install --upgrade && tendril --health        # sanity
vim VERSION CHANGELOG.md README.md             # 0.2.1 / v0.2.1 badge
git commit -am "chore(release): v0.2.1" && git push origin release/v0.2.1
# merge to main via PR, then:
git tag v0.2.1 && git push origin v0.2.1       # after the PR merges

# link repo: bump versionName to 0.2.1 first (app/build.gradle.kts),
# merge to main, then:
scripts/build-release.sh                        # tests + signed release build
gh release create v0.2.1 dist/tendril-link.apk dist/tendril-link.json \
  --title "TENDRIL v0.2.1" --draft   # write the notes, review, then publish
```

The release assets are exactly two files: `tendril-link.apk` and
`tendril-link.json` (flat schema-1: `schema, package, versionCode,
versionName, build, sha256, size, signer_sha256`). `phone/setup.sh` and
`tendril --link-fetch` both download exactly these names.

## 6. Verify the published release

```sh
tendril --link-fetch v0.2.1        # host: downloads, verifies signer+sha, stages
# a fresh Termux profile:
curl -fsSL https://raw.githubusercontent.com/Ozymandes/TENDRIL/main/phone/setup.sh | sh
```

Both must succeed with the pinned signer. Then the physical S21 checklist.
