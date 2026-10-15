import { Issues } from "@/features/repositories/issues";

export default async function Page({ params }: { params: Promise<{ number: string }> }) {
  const { number } = await params;
  return <Issues key={number} number={number} />;
}
