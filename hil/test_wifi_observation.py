import copy,json,unittest
from wifi_observation import decode


class WifiObservationTests(unittest.TestCase):
    def records(self):
        meta=dict(schema=1,enabled=True,startup_error=0,samples=2,retained=2,overwritten=0,
                  first_sequence=1,last_sequence=2,capacity=256,tx_retries=None,send_to_air_us=None,channel_occupancy=None,
                  tx_done_registered=True,tx_done_available=True)
        row=dict(sequence=1,counter_begin_us=100,sample_begin_us=120,sample_end_us=140,
                 ps_error=0,ps_mode=0,ap_error=0,rssi_dbm=-37,primary_channel=6,secondary_channel=0,
                 bssid='01:02:03:04:05:06',tx_done_success=50,tx_done_failed=1)
        second=copy.deepcopy(row);second.update(sequence=2,counter_begin_us=140,sample_begin_us=1000140,sample_end_us=1000160)
        return meta,[row,second]
    def lines(self,meta,rows):return ['WIFI_OBSERVE BEGIN '+json.dumps(meta),*['WIFI_OBSERVE ROW '+json.dumps(r) for r in rows],f"WIFI_OBSERVE END last_sequence={meta['last_sequence']}"]
    def test_real_values_and_explicit_unavailable_metrics(self):
        m,r=self.records();d=decode(self.lines(m,r))
        self.assertTrue(d['available']);self.assertTrue(d['callback_activity_observed']);self.assertFalse(d['qualification'])
        self.assertIsNone(d['metadata']['send_to_air_us']);self.assertEqual(d['rows'][0]['tx_done_failed'],1)
    def test_missing_duplicate_reordered_and_bad_extent_fail(self):
        m,r=self.records()
        for rows in (r[:1],r+[r[1]],r[::-1]):
            with self.assertRaises(ValueError):decode(self.lines(m,rows))
        for key,value in [('samples',3),('overwritten',1),('first_sequence',0),('retained',True)]:
            changed=dict(m);changed[key]=value
            with self.assertRaises(ValueError):decode(self.lines(changed,r))
    def test_api_failure_must_not_invent_rssi_or_ps(self):
        m,r=self.records();r[0]['ps_error']=-1
        with self.assertRaises(ValueError):decode(self.lines(m,r))
        r[0]['ps_mode']=None;r[0]['ap_error']=123
        with self.assertRaises(ValueError):decode(self.lines(m,r))
        for key in ('rssi_dbm','primary_channel','secondary_channel','bssid'):r[0][key]=None
        self.assertTrue(decode(self.lines(m,r))['available'])
    def test_overwrite_is_reported_not_erased_or_called_complete(self):
        m,r=self.records();m.update(samples=257,retained=256,overwritten=1,first_sequence=2,last_sequence=257)
        template=r[0];r=[]
        for sequence in range(2,258):
            row=dict(template);row.update(sequence=sequence,counter_begin_us=sequence*100,
                sample_begin_us=sequence*100+20,sample_end_us=(sequence+1)*100);r.append(row)
        self.assertFalse(decode(self.lines(m,r))['retained_history_not_overwritten'])
    def test_disabled_empty_and_unsupported_metrics(self):
        m,_=self.records();m.update(enabled=False,samples=0,retained=0,overwritten=0,first_sequence=0,last_sequence=0,capacity=0,
                                  tx_done_registered=False,tx_done_available=False)
        self.assertFalse(decode(self.lines(m,[]))['available'])
        for key in ('tx_retries','send_to_air_us','channel_occupancy'):
            changed=dict(m);changed[key]=0
            with self.assertRaises(ValueError):decode(self.lines(changed,[]))
    def test_clock_discontinuity_and_counter_corruption_fail(self):
        m,r=self.records()
        for key,value in [('counter_begin_us',141),('sample_end_us',-1),('tx_done_failed',-1),('ps_mode',True)]:
            changed=copy.deepcopy(r);changed[1][key]=value
            with self.assertRaises(ValueError):decode(self.lines(m,changed))

    def test_extent_order_and_capability_flags_cannot_be_forged(self):
        m,r=self.records();lines=self.lines(m,r)
        for reordered in ([lines[-1],*lines[:-1]],lines[1:]+lines[:1],lines+lines,lines+['WIFI_OBSERVE ROW {}']):
            with self.assertRaises(ValueError):decode(reordered)
        for flags in (dict(tx_done_registered=False,tx_done_available=False),dict(tx_done_available=False),
                      dict(tx_done_available='false'),dict(startup_error=-1)):
            changed=dict(m,**flags)
            with self.assertRaises(ValueError):decode(self.lines(changed,r))

    def test_api_error_null_fields_are_required_not_merely_optional(self):
        m,r=self.records();r[0].update(ps_error=-1,ap_error=-1)
        fields=('ps_mode','rssi_dbm','primary_channel','secondary_channel','bssid')
        for key in fields:r[0][key]=None
        self.assertTrue(decode(self.lines(m,r))['available'])
        for key in fields:
            changed=copy.deepcopy(r);del changed[0][key]
            with self.assertRaises(ValueError):decode(self.lines(m,changed))


if __name__=='__main__':unittest.main()
