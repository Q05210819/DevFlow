import { RepositoryScope } from "@/features/repositories/repository-detail";

export default async function Layout({ children, params }: { children: React.ReactNode; params: Promise<{ repositoryId: string }> }) {
  const { repositoryId } = await params;
  return <RepositoryScope repositoryId={repositoryId}>{children}</RepositoryScope>;
}
