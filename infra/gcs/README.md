# Bucket configuration for the Knowledge Basket

Two files, applied once when the bucket is created (`docs/deploy.md` §10b) and re-applied
by hand whenever they change. They are committed rather than typed at a prompt because a
CORS list and a lifecycle policy are security-relevant configuration: reviewing a diff is
the only way anyone notices an origin being widened or a retention window shrinking.

`basket-cors.json` — the browser PUTs bytes straight to Cloud Storage, so the bucket must
name the origins allowed to do it. Three, and **no wildcard**: a permissive CORS policy on
a bucket holding one tenant's documents is the same class of mistake as a permissive CORS
policy on the API. `PUT` is the upload, `GET`/`HEAD` are the download; the two response
headers are the ones a signed upload URL pins.

`basket-lifecycle.json` — `AbortIncompleteMultipartUpload` after 1 day sweeps the residue
of an upload the browser abandoned, which is otherwise billed storage nobody can see.
`daysSinceNoncurrentTime: 30` expires *superseded* versions only; it never touches a live
object, and it is what makes versioning a recovery window rather than unbounded growth.

Neither file is read by the application. Applying them:

```bash
gcloud storage buckets update "gs://${PROJECT_ID}-basket" --cors-file=infra/gcs/basket-cors.json
gcloud storage buckets update "gs://${PROJECT_ID}-basket" --lifecycle-file=infra/gcs/basket-lifecycle.json
```
