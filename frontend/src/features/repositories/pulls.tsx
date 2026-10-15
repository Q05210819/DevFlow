"use client";

import { FactDetails, FactList } from "./issues";
export function Pulls({ number }: { number?: string }) {
  return number ? <FactDetails resource="pulls" endpoint={`/pulls/${encodeURIComponent(number)}`} identity={number} collections={["comments", "reviews", "review_comments", "files"]} /> : <FactList resource="pulls" endpoint="/pulls" title="PRs" />;
}
