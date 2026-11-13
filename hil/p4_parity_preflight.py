"""Host-only refusals before campaign registration; never acquire a lock/device."""
from pathlib import Path
from types import SimpleNamespace

from p4_parity import require


def host_checks(args,plan,manifest,identity):
    import boards
    import board_smoke as smoke
    from p4_step14 import restoring,restore_record
    boards.require_bench()  # Validate the runner's inherited lock; no acquisition.
    restore_record(plan['restore_record'],plan['restore_record_sha256'])
    smoke.live_bot_identity(args.bot_dir,args.bot_repo,identity)
    require((args.bot_dir/'bot.log').is_file(),'launch the bot and retain its log before acquisition')
    # Constructors and these preflights perform imports/tool/version/bundle
    # checks only. No serial open, flash, audio or restore hold is entered.
    values=dict(vars(args));values.update(p4_restore_bundle=None,p4_restore_elf=None,
        p4_factory_backup=getattr(args,'p4_factory_backup',smoke.FACTORY_BACKUP))
    with restoring(plan['restore_record'],plan['restore_record_sha256']):
        io=smoke.LiveIO(SimpleNamespace(**values),args.out,manifest,identity)
        io.preflight_tools();io.preflight_restore()
