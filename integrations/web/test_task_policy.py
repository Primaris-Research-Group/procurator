import unittest
from task_policy import routine_allowed,normalize_task,CVS_GUEST_DIRECTIVE

def control(**values):
    return {'tag':'input','type':'text','value':'','label':'Name','href':'','role':'',**values}
class Tests(unittest.TestCase):
    def setUp(self):self.spec={'fields':[{'label':'Name','kind':'fill','value':'Practice Visitor'}],'options':['31901, GA'],'read_urls':['https://example.com/form']}
    def allowed(self,step):return routine_allowed(self.spec,{'steps':[step]})
    def test_exact_field_and_value(self):
        self.assertTrue(self.allowed({'kind':'fill','control':control(),'value':'Practice Visitor'}))
        self.assertFalse(self.allowed({'kind':'fill','control':control(),'value':'Different'}))
        self.assertFalse(self.allowed({'kind':'fill','control':control(label='Other'),'value':'Practice Visitor'}))
    def test_exact_navigation_only(self):
        self.assertTrue(self.allowed({'kind':'navigate','url':'https://example.com/form'}))
        self.assertFalse(self.allowed({'kind':'navigate','url':'https://example.com/form?secret=other'}))
    def test_option_needs_real_option_role(self):
        self.assertTrue(self.allowed({'kind':'click','control':control(tag='li',role='option',label='31901, GA')}))
        self.assertFalse(self.allowed({'kind':'click','control':control(tag='button',type='button',label='31901, GA')}))
    def test_submission_and_ambiguous_buttons_never_routine(self):
        for kind,label in [('submit','Search'),('button','Continue'),('button','Submit'),('button','Confirm appointment')]:
            self.assertFalse(self.allowed({'kind':'click','control':control(tag='button',type=kind,label=label)}))
    def test_consent_and_sensitive_controls_not_routine(self):
        for kind in ['password','file','checkbox','radio']:
            self.assertFalse(self.allowed({'kind':'click','control':control(type=kind,role='option',label='31901, GA')}))
        self.assertFalse(self.allowed({'kind':'fill','control':control(value='[manual entry only]'),'value':'Practice Visitor'}))
    def test_mixed_plan_denied_if_any_step_outside_grant(self):
        self.assertFalse(routine_allowed(self.spec,{'steps':[{'kind':'fill','control':control(),'value':'Practice Visitor'},{'kind':'click','control':control(tag='button',type='submit',label='Submit')}]}))
    def test_routine_next_requires_exact_owner_control_and_exact_page(self):
        item={'label':'Next','url':'https://example.com/form','tag':'button','type':'button','role':''}
        self.spec['clicks']=[item]
        plan={'url':item['url'],'steps':[{'kind':'click','control':control(tag='button',type='button',label='Next')}]}
        self.assertTrue(routine_allowed(self.spec,plan))
        self.assertFalse(routine_allowed(self.spec,plan|{'url':'https://example.com/consent'}))
        plan['steps'][0]['control']['type']='submit'
        self.assertFalse(routine_allowed(self.spec,plan))
    def test_exact_radio_is_routine_but_unknown_checked_consent_blocks_next(self):
        page='https://example.com/form'
        self.spec['clicks']=[{'label':'Male','url':page,'tag':'input','type':'radio','role':''},
            {'label':'Next','url':page,'tag':'button','type':'button','role':''}]
        self.assertTrue(routine_allowed(self.spec,{'url':page,'steps':[{'kind':'click','control':control(type='radio',label='Male')}]}))
        plan={'url':page,'steps':[{'kind':'click','control':control(tag='button',type='button',label='Next')}],
            'visible_form_values':[{'label':'Male','checked':True}]}
        self.assertTrue(routine_allowed(self.spec,plan))
        plan['visible_form_values'].append({'label':'Agree to terms','checked':True})
        self.assertFalse(routine_allowed(self.spec,plan))
    def test_visible_site_error_requires_a_fresh_review_even_for_granted_next(self):
        page='https://example.com/form'
        self.spec['clicks']=[{'label':'Next','url':page,'tag':'button','type':'button','role':''}]
        plan={'url':page,'page_errors':["Something's not right"],'steps':[{'kind':'click','control':control(tag='button',type='button',label='Next')}]}
        self.assertFalse(routine_allowed(self.spec,plan))
    def test_exact_owner_reviewed_cvs_guest_choice_does_not_need_repeated_permission(self):
        self.spec.update({'origin':'https://www.cvs.com','goal':'Book the approved flu shot. '+CVS_GUEST_DIRECTIVE+' Ask for missing facts.'})
        plan={'url':'https://www.cvs.com/scheduling/patient-lookup?lob=rximz&flow=vaccine',
            'title':'Send verification code - CVS','steps':[{'kind':'click','control':control(tag='a',type='',label='Continue as a guest',href='javascript:void(0)')} ]}
        self.assertTrue(routine_allowed(self.spec,plan))
        for changed in [plan|{'url':'https://other.example/scheduling/patient-lookup'},plan|{'title':'Other page'},plan|{'page_errors':['Error']}]:
            self.assertFalse(routine_allowed(self.spec,changed))
        for goal in ['Book flu shot.','Do not '+CVS_GUEST_DIRECTIVE]:
            self.assertFalse(routine_allowed(self.spec|{'goal':goal},plan))
        for href in ['javascript:sendCode()','javascript:void(fetch("https://other.example"))','data:text/html,test']:
            c=control(tag='a',type='',label='Continue as a guest',href=href)
            self.assertFalse(routine_allowed(self.spec,plan|{'steps':[{'kind':'click','control':c}]}))
        c=control(tag='button',type='button',label='Send code')
        self.assertFalse(routine_allowed(self.spec,plan|{'steps':[{'kind':'click','control':c}]}))
    def test_owner_grant_cannot_turn_commitment_or_authentication_into_routine(self):
        session={'practice':False,'origin':'https://example.com','id':'test','timeout_seconds':2700}
        def validate(value):return value,'https://example.com'
        for label,kind in [('Confirm appointment','button'),('Next','submit'),('Agree','checkbox'),('Send code','button'),('Sign in','button')]:
            args={'title':'Form task','goal':'Enter exact patient details','clicks':[{'label':label,'url':'https://example.com/form','tag':'button','type':kind,'role':''}]}
            with self.assertRaises(ValueError):normalize_task(args,session,validate,lambda v,_:v)
    def test_birth_display_can_be_explicitly_granted_without_granting_consent_checkboxes(self):
        page='https://www.cvs.com/scheduling/patient-lookup?lob=rximz&flow=vaccine'
        session={'practice':False,'origin':'https://www.cvs.com','id':'test','timeout_seconds':7200}
        def validate(value):return value,'https://www.cvs.com'
        show={'label':'Show date of birth','url':page,'tag':'input','type':'checkbox','role':''}
        next_control={'label':'Next','url':page,'tag':'button','type':'button','role':''}
        args={'title':'Patient lookup','goal':'Use approved details','fields':[{'label':'Date of birth','kind':'fill','value':'01011990'}],'clicks':[show,next_control]}
        spec=normalize_task(args,session,validate,lambda v,_:v)
        self.assertTrue(routine_allowed(spec,{'url':page,'steps':[{'kind':'click','control':control(type='checkbox',label='Show date of birth')}]}))
        self.assertTrue(routine_allowed(spec,{'url':page,'steps':[{'kind':'click','control':control(tag='button',type='button',label='Next')}],
            'visible_form_values':[{'label':'Show date of birth','checked':True}]}))
        with self.assertRaises(ValueError):normalize_task(args|{'clicks':[show|{'label':'Agree to terms'}]},session,validate,lambda v,_:v)
    def test_nine_patient_contact_fields_fit_a_task_but_unbounded_profiles_do_not(self):
        session={'practice':False,'origin':'https://example.com','id':'test'}
        args={'title':'Patient form','goal':'Enter approved data','fields':[{'label':'Field '+str(i),'kind':'fill','value':'value'} for i in range(9)]}
        self.assertEqual(len(normalize_task(args,session,lambda v:(v,'https://example.com'),lambda v,_:v)['fields']),9)
        args['fields']*=2
        with self.assertRaises(ValueError):normalize_task(args,session,lambda v:(v,'https://example.com'),lambda v,_:v)
    def test_cvs_flu_picker_needs_exact_reviewed_control_and_page(self):
        page='https://www.cvs.com/scheduling/vaccineselection?lob=rximz&flow=vaccine'
        session={'practice':False,'origin':'https://www.cvs.com','id':'test'}
        item={'label':'Flu','url':page,'tag':'button','type':'submit','role':''}
        args={'title':'Flu appointment','goal':'Select Flu only','clicks':[item]}
        spec=normalize_task(args,session,lambda v:(v,'https://www.cvs.com'),lambda v,_:v)
        plan={'url':page,'steps':[{'kind':'click','control':control(tag='button',type='submit',label='Flu')}]}
        self.assertTrue(routine_allowed(spec,plan))
        self.assertFalse(routine_allowed(spec|{'clicks':[]},plan))
        self.assertFalse(routine_allowed(spec,plan|{'url':'https://www.cvs.com/scheduling/confirmation'}))
        self.assertFalse(routine_allowed(spec,plan|{'page_errors':['Error']}))
        self.assertFalse(routine_allowed(spec,plan|{'visible_form_values':[{'label':'Agree','checked':True}]}))
        for change in [{'label':'Confirm appointment'},{'label':'COVID-19'},{'url':page+'&other=1'},{'role':'button'}]:
            with self.assertRaises(ValueError):normalize_task(args|{'clicks':[item|change]},session,lambda v:(v,'https://www.cvs.com'),lambda v,_:v)
if __name__=='__main__':unittest.main()
