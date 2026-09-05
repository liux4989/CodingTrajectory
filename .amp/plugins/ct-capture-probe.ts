import type {
	PluginAPI,
	PluginThread,
	ThreadMessage,
} from '@ampcode/plugin'
import { appendFile, mkdir } from 'node:fs/promises'
import { join } from 'node:path'

export const description =
	'Captures sanitized Amp lifecycle and tool-event structure for one validation thread.'

const TARGET_THREAD_ID = 'T-01a07013-7763-72bb-b0ec-176c58048b83'
const PAGE_SIZE = 20

type EventName =
	| 'session.start'
	| 'agent.start'
	| 'tool.call'
	| 'tool.result'
	| 'agent.end'

function fieldNames(value: unknown): string[] {
	if (!value || typeof value !== 'object' || Array.isArray(value)) return []
	return Object.keys(value).sort()
}

function summarizeMessage(message: ThreadMessage) {
	return {
		id: message.id,
		role: message.role,
		field_names: fieldNames(message),
		block_count: message.content.length,
		blocks: message.content.map((block) => ({
			type: block.type,
			field_names: fieldNames(block),
			...('id' in block ? { tool_use_id: block.id } : {}),
			...('toolUseID' in block ? { tool_use_id: block.toolUseID } : {}),
			...('status' in block ? { status: block.status } : {}),
			...('input' in block ? { input_field_names: fieldNames(block.input) } : {}),
		})),
	}
}

async function transcriptStructure(thread: PluginThread) {
	const messages: ThreadMessage[] = []
	const pages: Array<{ offset: number; count: number }> = []

	for (let offset = 0; ; offset += PAGE_SIZE) {
		const page = await thread.messages({
			full: true,
			from: 'start',
			offset,
			limit: PAGE_SIZE,
		})
		pages.push({ offset, count: page.length })
		messages.push(...page)
		if (page.length < PAGE_SIZE) break
	}

	return {
		request: { full: true, from: 'start', limit: PAGE_SIZE },
		pages,
		message_count: messages.length,
		messages: messages.map(summarizeMessage),
	}
}

export default function ctCaptureProbe(amp: PluginAPI) {
	const workspaceRoot = amp.system.workspaceRoot
	if (!workspaceRoot) return

	const outputDirectory = join(
		amp.helpers.filePathFromURI(workspaceRoot),
		'.amp',
		'local',
		'ct-capture-probe',
	)
	const outputPath = join(outputDirectory, `${TARGET_THREAD_ID}.jsonl`)
	let writes = Promise.resolve()

	function capture(
		eventName: EventName,
		ctx: { thread: PluginThread },
		eventStructure: Record<string, unknown>,
	): Promise<void> {
		if (ctx.thread.id !== TARGET_THREAD_ID) return Promise.resolve()

		const write = writes.then(async () => {
			const observedAt = new Date().toISOString()
			const [parentThreadID, transcript] = await Promise.all([
				ctx.thread.parentThreadID(),
				transcriptStructure(ctx.thread),
			])
			await mkdir(outputDirectory, { recursive: true, mode: 0o700 })
			await appendFile(
				outputPath,
				`${JSON.stringify({
					schema_version: 1,
					event: eventName,
					observed_at: observedAt,
					thread_id: ctx.thread.id,
					parent_thread_id: parentThreadID,
					event_structure: eventStructure,
					transcript,
				})}\n`,
				{ encoding: 'utf8', mode: 0o600 },
			)
		})

		writes = write.catch((error) => {
			amp.logger.log(`ct-capture-probe ${eventName} capture failed`, error)
		})
		return writes
	}

	amp.on('session.start', (event, ctx) =>
		capture('session.start', ctx, {
			field_names: fieldNames(event),
			event_thread_id: event.thread.id,
		}),
	)
	amp.on('agent.start', async (event, ctx) => {
		await capture('agent.start', ctx, {
			field_names: fieldNames(event),
			message_id: event.id,
		})
		return {}
	})
	amp.on('tool.call', async (event, ctx) => {
		await capture('tool.call', ctx, {
			field_names: fieldNames(event),
			tool_use_id: event.toolUseID,
			input_field_names: fieldNames(event.input),
		})
		return { action: 'allow' }
	})
	amp.on('tool.result', async (event, ctx) => {
		await capture('tool.result', ctx, {
			field_names: fieldNames(event),
			tool_use_id: event.toolUseID,
			status: event.status,
			input_field_names: fieldNames(event.input),
			output_field_names: fieldNames(event.output),
			has_error: event.error !== undefined,
			has_output: event.output !== undefined,
		})
	})
	amp.on('agent.end', (event, ctx) =>
		capture('agent.end', ctx, {
			field_names: fieldNames(event),
			message_id: event.id,
			status: event.status,
			message_count: event.messages.length,
			message_ids: event.messages.map((message) => message.id),
		}),
	)
}
