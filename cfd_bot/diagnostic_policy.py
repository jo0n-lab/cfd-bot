"""Normal-call boundaries for basic diagnostics; helpers still report failures.

This is a logging policy only. It must never choose an application result.
"""
_GROUPS = {
    'artifacts': 'export_files freeze_exports residual_files',
    'bot.Bot': 'set_queue_selection show_queue_groups acknowledge_callback active_runs case_menu dispatch file fresh_runs handle queue_selection send show_cases show_queue_selection start_callback_ack status',
    'bot': 'deliver serve serve.monitor_loop serve.notification_loop',
    'catalog.TicketIndex': 'refresh changes acknowledge',
    'cli': 'main',
    'config': 'load_bot',
    'cpu_allocation': 'allocate_cpus',
    'editor.TicketService': 'delete delete_many deletion_preview duplicate listing new open save validate',
    'editor': 'validate_export',
    'execution': 'apply_execution_settings',
    'jobs.Scheduler': 'recover tick _claim_dynamic',
    'jobs': 'run_case_hooks run_hook terminal_event worker _stop_child',
    'logs': 'read_log recent_log finish_log',
    'monitor.Monitor': 'observe run_once tick',
    'outcomes': 'decide',
    'patterns.PatternLibrary': 'load save',
    'processes': 'snapshot DaemonLock.__enter__ DaemonLock.__exit__',
    'queue_control': 'cancel_queued_jobs interrupt_running_job interrupt_running_jobs interrupt_process_groups',
    'storage.Store': 'request_interruption acknowledge_ticket_changes cancel_queued clear_messages delivered enqueue enqueue_batch event finish_observation mark_terminal_published put remember_message remember_run retry save_delivery update_job',
    'telegram.Telegram': 'call delete_messages file send updates',
    'ticket_chat.TicketChat': 'action apply_field basic browser bulk_list card delete_confirmed delete_review do_switch draft export_card exports field forget_panels handle input listing load members persist queue render review rules save_template scan scan.work scripts set_browser_path switch templates',
    'ticket_run.TicketRunner': 'request state states',
    'tickets': 'accept_submissions atomic_json clone_document discover_cases publish_macro sync_ticket_states ticket_lock',
    'web.Handler': 'do_GET do_POST handle_request respond',
    'web.WebApp': 'artifacts browse detail file fresh get overview post ticket_rows case_rows',
    'web': 'main serve',
    'gui': 'launch main',
}
BASIC_FUNCTIONS = frozenset(prefix + '.' + name for prefix, names in _GROUPS.items() for name in names.split())
_GUI_DETAILS = frozenset('field multiline multiline.next_field table tab tab.reveal tab.wheel values document event_checks render_case_rows refresh_tables refresh_patterns update_execution_button update_execution_visibility update_execution_visibility.set_enabled _queue_duration callback:L327 callback:L328'.split())


def basic_function(name):
    if name.startswith('gui.TicketEditor.'):
        return name.removeprefix('gui.TicketEditor.') not in _GUI_DETAILS
    return name in BASIC_FUNCTIONS
