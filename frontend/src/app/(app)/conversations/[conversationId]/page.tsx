import { ConversationDetail } from "@/features/chat/chat-page";

export default async function ConversationPage({ params }: { params: Promise<{ conversationId: string }> }) {
  const { conversationId } = await params;
  return <main className="content"><span className="eyebrow">工作台 / 私有会话</span><ConversationDetail key={conversationId} conversationId={conversationId} /></main>;
}
