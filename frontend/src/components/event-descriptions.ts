import type { EventKind } from '../../../contracts/types';
import type { Language } from '../ui-preferences';

const descriptions: Record<EventKind, Record<Language, string>> = {
  userMessage: {
    zh: '用户输入是用户发给助手的消息，包含任务目标、问题、补充信息或反馈。',
    en: 'User messages contain requests, questions, additional context, or feedback sent to the assistant.',
  },
  assistantMessage: {
    zh: '助手输出是助手生成并记录的回复，可能是进度说明、解释或最终结果。',
    en: 'Assistant messages are recorded replies, including progress updates, explanations, and final results.',
  },
  reasoningSummary: {
    zh: '思考活动记录包含已保存的公开摘要和活动时间；未提供公开摘要时只展示时间，不重建私有推理内容。',
    en: 'Reasoning activity records contain recorded timing and any public summary. When no public summary is provided, only timing is shown; private reasoning is not reconstructed.',
  },
  modelUsage: {
    zh: '模型用量是某次明确 response_id 的输入、输出与缓存 token 记录。一个请求可以生成多条消息或工具调用，因此用量单独列出，不重复分摊到这些条目。',
    en: 'Model usage records input, output, and cached tokens for an explicit response ID. A request can produce multiple messages or tool calls, so its usage is listed separately without duplicating it across those entries.',
  },
  systemContext: {
    zh: '系统说明是助手运行时的基础指令，用于规定角色、通用行为规则和能力边界。',
    en: 'System context provides the foundational instructions that define the assistant’s role, general behavior, and boundaries.',
  },
  developerContext: {
    zh: '开发者说明是应用或开发环境提供给助手的工作指引，用于约定行为规则、工具使用方式和任务约束。',
    en: 'Developer context provides instructions from the application or development environment about behavior, tool use, and task constraints.',
  },
  environment: {
    zh: '运行环境记录任务执行时的上下文，例如工作目录、模型配置、权限设置和当前时间。',
    en: 'Environment records describe the task’s execution context, such as its working directory, model configuration, permissions, and current time.',
  },
  toolCall: {
    zh: '工具调用是助手向工具发起的操作请求，记录所用工具及传入参数，例如执行命令、读取文件或查询信息。',
    en: 'A tool call is an operation requested by the assistant. It records the tool and its arguments, for actions such as running commands, reading files, or looking up information.',
  },
  toolResult: {
    zh: '工具结果是工具执行后返回的信息，可能包含输出内容、执行状态或错误信息。',
    en: 'A tool result contains information returned by a tool, which may include output, execution status, or error details.',
  },
  compaction: {
    zh: '上下文压缩会将较早的对话整理为摘要，以便在有限的上下文空间中继续任务。这里展示压缩时保存的记录。',
    en: 'Context compaction summarizes earlier conversation so the task can continue within the context limit. This record contains information saved during compaction.',
  },
  collaboration: {
    zh: '协作操作记录助手与其他 Agent 之间的任务分配、消息传递或进度等待等活动。',
    en: 'Collaboration records capture activities between the assistant and other agents, such as assigning work, exchanging messages, or waiting for progress.',
  },
};

export function eventKindDescription(kind: EventKind, language: Language = 'zh'): string {
  return descriptions[kind][language];
}
