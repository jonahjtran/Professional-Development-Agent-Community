import { Link, useParams } from "react-router-dom";
import { Banner, Card, Loading, Rendered } from "../components/ui";
import { useLoad } from "../hooks";
import type { Artifact } from "../types";

export function ArtifactPage() {
  const { slug = "", "*": name = "" } = useParams();
  const { data, error } = useLoad<Artifact>(`/api/projects/${slug}/artifacts/${name}`);
  return (
    <>
      <Link className="back" to={`/p/${slug}`}>‹ {slug}</Link>
      {error && <Banner>{error.status === 404 ? "There is no such document." : error.message}</Banner>}
      {data === null && !error && <Loading what="the document" />}
      {data !== null && (
        <>
          <h1>{data.name}</h1>
          <p className="lede">
            Revision {data.revision} ·{" "}
            {data.approved_at
              ? `approved ${new Date(data.approved_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`
              : "not approved yet"}
          </p>
          {data.kind === "markdown" && data.html !== null ? (
            <Rendered html={data.html} />
          ) : (
            <Card><pre>{data.text}</pre></Card>
          )}
        </>
      )}
    </>
  );
}
