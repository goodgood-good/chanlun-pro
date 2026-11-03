from cl_app.services.screening_workbench import _dashboard_projection


def test_completed_processing_reports_verified_and_failed_coverage_separately():
    payload={'run_id':'a'*32,'status':'completed','selected':[],'observations':[],
        'settings':{'frequencies':['5m','30m']},
        'processed_periods':[{'market':'a','code':'SH.600001','frequency':'5m'},
                             {'market':'fx','code':'FE.USDCNY','frequency':'5m'}],
        'verified_periods':[{'market':'a','code':'SH.600001','frequency':'5m'}],
        'errors':[{'market':'fx','code':'FE.USDCNY','frequency':'5m','reasons':['SESSION_CALENDAR_UNAVAILABLE']}]}
    result=_dashboard_projection(payload,True)
    assert result['input_coverage']=={'processed_periods':2,'verified_periods':1,
                                     'failed_periods':1,'unverified_contract_symbols':1}
    assert result['_workbench_processed_periods']==[]
