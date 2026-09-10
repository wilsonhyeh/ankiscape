"""Real Qt button/lifetime regressions. No network or mail is used."""
def run(step, shot):
    from aqt import mw
    from aqt.qt import QApplication, QDialogButtonBox, QLineEdit, QTimer, QWidget, QPushButton, QLabel
    from ankiscape.evolved.ui.onboarding import build_onboarding_screen
    from ankiscape.evolved import onboarding
    from ankiscape.evolved.data import load_rules
    from ankiscape.evolved.ui import dialogs
    QApplication.instance().setQuitOnLastWindowClosed(False)
    rules = load_rules()
    class Host(QWidget):
        def __init__(self):
            super().__init__()
            self.draft = onboarding.OnboardingState()
        def call(self, name, *args, default=None):
            if name == 'get_rules': return rules
            if name == 'get_onboarding': return self.draft.to_dict()
            if name == 'save_onboarding': self.draft = onboarding.from_dict(args[0]); return {'ok':True}
            if name == 'advance_onboarding': onboarding.advance(self.draft, rules)
            if name == 'back_onboarding': onboarding.back(self.draft)
            return default
    host = Host()
    host.resize(900,680)
    host.show()
    screen = build_onboarding_screen(host,{})
    screen.show()
    def click(name, expected):
        button = next((b for b in screen.findChildren(QPushButton) if b.objectName() == name and b.isVisible()), None)
        assert button and button.isVisible(), name
        button.click()
        QApplication.processEvents()
        assert host.draft.step == expected, (name, host.draft.step, expected)
    primary = 'ankiscape-onboarding-primary'
    back = 'ankiscape-onboarding-back'
    for _ in range(2):
        click(primary,'skill')
        click(back,'welcome')
    click(primary,'skill')
    click('ankiscape-onboarding-skill-mining','resource')
    click(back,'skill')
    click(back,'welcome')
    click(primary,'skill')
    click('ankiscape-onboarding-skill-fishing','resource')
    click(primary,'explain')
    click(back,'resource')
    click(primary,'explain')
    assert host.draft.starting_resource == 'Shrimp'
    step('regression_setup_back_forward_order',True)
    screen.close(); screen.deleteLater(); host.deleteLater()

    def automate(name, fields, retry=False):
        def drive():
            dlg = QApplication.activeModalWidget()
            if dlg is None or dlg.objectName() != name:
                QTimer.singleShot(50,drive); return
            for obj,value in fields.items():
                edit=dlg.findChild(QLineEdit,obj)
                assert edit is not None,obj
                edit.setText(value)
            buttons=dlg.findChild(QDialogButtonBox)
            buttons.button(QDialogButtonBox.StandardButton.Ok).click()
            if retry:
                assert dlg.isVisible()
                assert dlg.findChild(QLabel,'ankiscape-error-label').text() == 'Try again'
                buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        QTimer.singleShot(60,drive)
    seen=[]
    def submit(data):
        seen.append(dict(data))
        return {'ok':len(seen)>1,'error':'Try again'}
    automate('ankiscape-login-dialog',{'ankiscape-login-identity':'test_user','ankiscape-login-password':'example-only'},True)
    result=dialogs.show_login_dialog(mw,submit)
    assert result['ok'] and len(seen)==2 and seen[1]['identity']=='test_user'
    step('regression_real_login_ok_and_retry',True)
    seen=[]
    automate('ankiscape-register-dialog',{'ankiscape-register-username':'tester','ankiscape-register-email':'test@example.invalid','ankiscape-register-password':'example-only'})
    result=dialogs.show_register_dialog(mw,lambda data:(seen.append(data) or {'ok':True}))
    assert result['ok'] and seen[0]['username']=='tester'
    step('regression_real_register_ok',True)
    requests=[]; confirms=[]
    automate('ankiscape-recovery-request',{'ankiscape-recovery-email':'test@example.invalid'})
    def request(email):
        requests.append(email)
        automate('ankiscape-recovery-dialog',{'ankiscape-email-code':'123456','ankiscape-recovery-password':'example-only'})
        return {'ok':True}
    result=dialogs.show_recovery_dialog(mw,request,lambda data:(confirms.append(data) or {'ok':True}))
    assert result['ok'] and len(requests)==1 and confirms[0]['code']=='123456'
    step('regression_recovery_request_then_confirm',True)
    from ankiscape.evolved.ui.guide import build_guide_screen
    host=Host(); guide=build_guide_screen(host,{})
    guide.resize(800,600); guide.show()
    from aqt.qt import QComboBox, QTextBrowser
    topics=guide.findChild(QComboBox)
    browser=guide.findChild(QTextBrowser)
    for i in range(topics.count()):
        topics.setCurrentIndex(i)
        assert len(browser.toPlainText())>100
    step('regression_all_guide_articles_render',True)
    guide.close(); guide.deleteLater(); host.deleteLater()
    # Real journal restore, with injected file/confirmation choices only.
    import json, tempfile, uuid
    from pathlib import Path
    import ankiscape as addon
    from ankiscape.evolved.backup import export_backup
    from aqt.qt import QFileDialog
    import aqt.utils
    old_pointer=mw.col.get_config('ankiscape_evolved_player_data')
    old_dialog=QFileDialog.getOpenFileName
    old_ask=aqt.utils.askUser
    old_onboarding=addon._EVOLVED_CTX.get('onboarding')
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory,'backup.json')
        other=str(uuid.uuid4())
        path.write_text(json.dumps(export_backup(other,{'operations':[],'observations':[]})))
        QFileDialog.getOpenFileName=lambda *a,**k:(str(path),'JSON')
        try:
            aqt.utils.askUser=lambda *a,**k:False
            result=addon._evolved_restore_backup()
            assert result['error']=='cancelled'
            assert mw.col.get_config('ankiscape_evolved_player_data')==old_pointer
            assert not Path(mw.pm.profileFolder(),'ankiscape-evolved',other).exists()
            step('regression_restore_cancel_before_write',True)
            aqt.utils.askUser=lambda *a,**k:True
            result=addon._evolved_restore_backup()
            assert result['ok']
            assert mw.col.get_config('ankiscape_evolved_player_data')['game_uuid']==other
            assert addon._EVOLVED_CTX['engine'].cfg.game_uuid==other
            step('regression_restore_activates_selected_game',True)
        finally:
            QFileDialog.getOpenFileName=old_dialog
            aqt.utils.askUser=old_ask
            engine=addon._EVOLVED_CTX.get('engine')
            if engine: engine.journal.close()
            mw.col.set_config('ankiscape_evolved_player_data',old_pointer,undoable=False)
            addon._EVOLVED_CTX.update(engine=None,journal=None,game_uuid=None,onboarding=old_onboarding)
            addon._ensure_evolved_engine()
