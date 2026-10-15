import { Pulls } from "@/features/repositories/pulls";

export default async function Page({ params }: { params: Promise<{ number: string }> }) {
  const { number } = await params;
  return <Pulls key={number} number={number} />;
}
