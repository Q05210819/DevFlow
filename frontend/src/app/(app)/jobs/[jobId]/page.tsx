import { JobView } from "@/features/jobs/job-view";

export default async function Page({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await params;
  return <JobView key={jobId} jobId={jobId} />;
}
